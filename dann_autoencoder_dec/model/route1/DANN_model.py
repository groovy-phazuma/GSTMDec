import os
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.utils.data as Data
import random
import numpy as np
import pandas as pd
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

from model.utils import *

class EncoderBlock(nn.Module):
    def __init__(self, in_dim, out_dim, do_rates):
        super(EncoderBlock, self).__init__()
        self.layer = nn.Sequential(nn.Linear(in_dim, out_dim),
                                   nn.LeakyReLU(0.2, inplace=True),
                                   nn.Dropout(p=do_rates, inplace=False))
    def forward(self, x):
        out = self.layer(x)
        return out

class DecoderBlock(nn.Module):
    def __init__(self, in_dim, out_dim, do_rates):
        super(DecoderBlock, self).__init__()
        self.layer = nn.Sequential(nn.Linear(in_dim, out_dim),
                                   nn.LeakyReLU(0.2, inplace=True),
                                   nn.Dropout(p=do_rates, inplace=False))
    def forward(self, x):
        out = self.layer(x)
        return out

class DANN(object):
    def __init__(self, option_list,celltype_num=None,labels=None,used_features=None,seed=2021):
        self.num_epochs = option_list['epochs']
        self.batch_size = option_list['batch_size']
        self.target_type = option_list['target_type']
        self.learning_rate = option_list['learning_rate']
        self.celltype_num = None
        self.labels = None
        self.used_features = None
        self.seed = seed
        self.outdir = option_list['SaveResultsDir']

        cudnn.deterministic = True
        torch.cuda.manual_seed_all(self.seed)
        torch.manual_seed(self.seed)
        random.seed(self.seed)

    def DANN_model(self, celltype_num):
        feature_num = len(self.used_features)
        
        self.encoder_da = nn.Sequential(EncoderBlock(feature_num, 512, 0), 
                                        EncoderBlock(512, 256, 0.3))

        self.predictor_da = nn.Sequential(EncoderBlock(256, 128, 0.2), 
                                          nn.Linear(128, celltype_num), 
                                          nn.Softmax(dim=1))
        
        self.discriminator_da = nn.Sequential(EncoderBlock(256, 128, 0.2), 
                                              nn.Linear(128, 1), 
                                              nn.Sigmoid())

        model_da = nn.ModuleList([])
        model_da.append(self.encoder_da)
        model_da.append(self.predictor_da)
        model_da.append(self.discriminator_da)
        return model_da

    def prepare_dataloader(self, source_data, target_data, batch_size):
        ### Prepare data loader for training ###
        # Source dataset
        source_ratios = [source_data.obs[ctype] for ctype in source_data.uns['cell_types']]
        self.source_data_x = source_data.X.astype(np.float32)
        self.source_data_y = np.array(source_ratios, dtype=np.float32).transpose()
        
        tr_data = torch.FloatTensor(self.source_data_x)
        tr_labels = torch.FloatTensor(self.source_data_y)
        source_dataset = Data.TensorDataset(tr_data, tr_labels)
        self.train_source_loader = Data.DataLoader(dataset=source_dataset, batch_size=batch_size, shuffle=True)

        # Extract celltype and feature info
        self.labels = source_data.uns['cell_types']
        self.celltype_num = len(self.labels)
        self.used_features = list(source_data.var_names)

        # Target dataset
        self.target_data_x = target_data.X.astype(np.float32)
        if self.target_type == "simulated":
            target_ratios = [target_data.obs[ctype] for ctype in self.labels]
            self.target_data_y = np.array(target_ratios, dtype=np.float32).transpose()
        elif self.target_type == "real":
            self.target_data_y = np.random.rand(target_data.shape[0], self.celltype_num)

        te_data = torch.FloatTensor(self.target_data_x)
        te_labels = torch.FloatTensor(self.target_data_y)
        target_dataset = Data.TensorDataset(te_data, te_labels)
        self.train_target_loader = Data.DataLoader(dataset=target_dataset, batch_size=batch_size, shuffle=True)
        self.test_target_loader = Data.DataLoader(dataset=target_dataset, batch_size=batch_size, shuffle=False)

    def train(self, source_data, target_data):

        ### prepare model structure ###
        self.prepare_dataloader(source_data, target_data, self.batch_size)
        self.model_da = self.DANN_model(self.celltype_num).cuda()

        ### setup optimizer ###
        optimizer_da1 = torch.optim.Adam([{'params': self.encoder_da.parameters()},
                                          {'params': self.predictor_da.parameters()},
                                          {'params': self.discriminator_da.parameters()}], lr=self.learning_rate)
        optimizer_da2 = torch.optim.Adam([{'params': self.encoder_da.parameters()},
                                          {'params': self.discriminator_da.parameters()}], lr=self.learning_rate)
        
        criterion_da = nn.BCELoss().cuda()
        source_label = torch.ones(self.batch_size).unsqueeze(1).cuda()   # define source domain label as 1
        target_label = torch.zeros(self.batch_size).unsqueeze(1).cuda()  # define target domain label as 0
        
        self.metric_logger = defaultdict(list) 

        best_pred_loss = 1e10
        update_flag = 0
        for epoch in range(self.num_epochs):
            self.model_da.train()

            train_target_iterator = iter(self.train_target_loader)
            pred_loss_epoch, disc_loss_epoch, disc_loss_DA_epoch = 0., 0., 0.
            for batch_idx, (source_x, source_y) in enumerate(self.train_source_loader):
                # get batch item of target
                try:
                    target_x, _ = next(train_target_iterator)
                except StopIteration:
                    train_target_iterator = iter(self.train_target_loader)
                    target_x, _ = next(train_target_iterator)

                embedding_source = self.encoder_da(source_x.cuda())
                embedding_target = self.encoder_da(target_x.cuda())
                frac_pred = self.predictor_da(embedding_source)
                domain_pred_source = self.discriminator_da(embedding_source)
                domain_pred_target = self.discriminator_da(embedding_target)

                # caculate loss 
                pred_loss = L1_loss(frac_pred, source_y.cuda())       
                pred_loss_epoch += pred_loss.data.item()
                disc_loss = criterion_da(domain_pred_source, source_label[0:domain_pred_source.shape[0],]) + criterion_da(domain_pred_target, target_label[0:domain_pred_target.shape[0],])
                disc_loss_epoch += disc_loss.data.item()
                loss = pred_loss + disc_loss

                # update weights
                optimizer_da1.zero_grad()
                loss.backward(retain_graph=True)
                optimizer_da1.step()

                embedding_source = self.encoder_da(source_x.cuda())
                embedding_target = self.encoder_da(target_x.cuda())
                domain_pred_source = self.discriminator_da(embedding_source)
                domain_pred_target = self.discriminator_da(embedding_target)

                # caculate loss 
                disc_loss_DA = criterion_da(domain_pred_target, source_label[0:domain_pred_target.shape[0],]) + criterion_da(domain_pred_source, target_label[0:domain_pred_source.shape[0],]) 
                disc_loss_DA_epoch += disc_loss_DA.data.item()

                # update weights
                optimizer_da2.zero_grad()
                disc_loss_DA.backward(retain_graph=True)
                optimizer_da2.step()

            pred_loss_epoch = pred_loss_epoch/(batch_idx + 1)
            self.metric_logger['pred_loss'].append(pred_loss_epoch)
            disc_loss_epoch = disc_loss_epoch/(batch_idx + 1)
            self.metric_logger['disc_loss'].append(disc_loss_epoch)
            disc_loss_DA_epoch = disc_loss_DA_epoch/(batch_idx + 1)
            self.metric_logger['disc_loss_DA'].append(disc_loss_DA_epoch)
        
            if (epoch+1) % 10 == 0:
                print('============= Epoch {:02d}/{:02d} ============='.format(epoch + 1, self.num_epochs))
                print("pred_loss=%f, disc_loss=%f, disc_loss_DA=%f" % (pred_loss_epoch, disc_loss_epoch, disc_loss_DA_epoch))
                if self.target_type == "simulated":
                    ### model validation on target data ###
                    target_preds, ground_truth = self.prediction()
                    epoch_ccc, epoch_rmse, epoch_corr = compute_metrics(target_preds, ground_truth)
                    self.metric_logger['target_ccc'].append(epoch_ccc)
                    self.metric_logger['target_rmse'].append(epoch_rmse)
                    self.metric_logger['target_corr'].append(epoch_corr)
                
                # save checkpoint
                if pred_loss_epoch < best_pred_loss:
                    best_pred_loss = pred_loss_epoch  # update
                    update_flag = 0
                    torch.save(self.model_da.state_dict(), os.path.join(self.outdir, 'best_model.pth'))
                    self.metric_logger['best_epoch'] = epoch + 1
                    print("Save model at epoch %d" % (epoch+1))
                else:
                    update_flag += 1
                    # early stopping
                    if update_flag == 10:
                        print("Early stopping at epoch %d" % (epoch+1))
                        break

        """ TODO: remove
        if self.target_type == "simulated":
            SaveLossPlot(self.outdir, self.metric_logger, loss_type = ['pred_loss','disc_loss','disc_loss_DA','target_ccc','target_rmse','target_corr'], output_prex = 'Loss_metric_plot_stage3')
        elif self.target_type == "real":
            SaveLossPlot(self.outdir, self.etric_logger, loss_type = ['pred_loss','disc_loss','disc_loss_DA'], output_prex = 'Loss_metric_plot_stage3')
        """
    
    def load_checkpoint(self, model_path):
        self.model_da = self.DANN_model(self.celltype_num).cuda()
        self.model_da.load_state_dict(torch.load(model_path))
        self.model_da.eval()
            
    def prediction(self):
        self.model_da.eval()
        preds, gt = None, None
        for batch_idx, (x, y) in enumerate(self.test_target_loader):
            logits = self.predictor_da(self.encoder_da(x.cuda())).detach().cpu().numpy()
            frac = y.detach().cpu().numpy()
            preds = logits if preds is None else np.concatenate((preds, logits), axis=0)
            gt = frac if gt is None else np.concatenate((gt, frac), axis=0)

        target_preds = pd.DataFrame(preds, columns=self.labels)
        ground_truth = pd.DataFrame(gt, columns=self.labels)  # random ratio is output if "real"
        return target_preds, ground_truth
    
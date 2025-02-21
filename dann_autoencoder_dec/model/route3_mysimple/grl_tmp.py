# -*- coding: utf-8 -*-
"""
Created on 2025-02-21 (Fri) 09:06:45

Domain adaptation with Gradient Reversal Layer (GRL)

@author: I.Azuma
"""
import os
import random
import numpy as np
import pandas as pd
from tqdm import tqdm
from collections import defaultdict
from sklearn.metrics import roc_auc_score

import torch
import torch.nn as nn
import torch.utils.data as Data
import torch.nn.functional  as F
import torch.backends.cudnn as cudnn

from torchviz import make_dot
from torch.utils.data import DataLoader
from torch.utils.data.dataset import TensorDataset


import warnings
warnings.filterwarnings('ignore')

from model.utils import *

import sys
BASE_DIR = '/workspace/mnt/cluster/HDD/azuma/TopicModel_Deconv'
sys.path.append(BASE_DIR+'/github/GSTMDec')
from _utils import common_utils

class LossFunctions:
    eps = 1e-8

    def reconstruction_loss(self, real, predicted, dropout_mask=None, rec_type='mse'):
        if rec_type == 'mse':
            if dropout_mask is None:
                loss = torch.mean((real - predicted).pow(2))
            else:
                loss = torch.sum((real - predicted).pow(2) * dropout_mask) / torch.sum(dropout_mask)
        elif rec_type == 'bce':
            loss = F.binary_cross_entropy(predicted, real, reduction='none').mean()
        else:
            raise Exception
        return loss
    
    def summarize_loss(self, theta_tensor, prop_tensor):
        # deconvolution loss
        assert theta_tensor.shape[0] == prop_tensor.shape[0], "Batch size is different"
        deconv_loss_dic = common_utils.calc_deconv_loss(theta_tensor, prop_tensor)
        deconv_loss = deconv_loss_dic['cos_sim'] + 0.0*deconv_loss_dic['rmse']

        return deconv_loss
    
    def L1_loss(self, preds, gt):
        loss = torch.mean(torch.reshape(torch.square(preds - gt), (-1,)))
        return loss


class EncoderBlock(nn.Module):
    def __init__(self, in_dim, out_dim, do_rates):
        super(EncoderBlock, self).__init__()
        self.layer = nn.Sequential(nn.Linear(in_dim, out_dim),
                                   #nn.BatchNorm1d(out_dim),
                                   nn.LeakyReLU(0.2, inplace=True),
                                   nn.Dropout(p=do_rates, inplace=False))
    def forward(self, x):
        out = self.layer(x)
        return out

class DecoderBlock(nn.Module):
    def __init__(self, in_dim, out_dim, do_rates):
        super(DecoderBlock, self).__init__()
        self.layer = nn.Sequential(nn.Linear(in_dim, out_dim),
                                   #nn.BatchNorm1d(out_dim),
                                   nn.LeakyReLU(0.2, inplace=True),
                                   nn.Dropout(p=do_rates, inplace=False))
    def forward(self, x):
        out = self.layer(x)
        return out

# GRL (Gradient Reversal Layer)
"""
class GradientReversalLayer(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, alpha):
        ctx.alpha = alpha
        return x

    @staticmethod
    def backward(ctx, grad_output):
        return -ctx.alpha * grad_output, None
"""

class GradientReversalLayer(torch.autograd.Function):
    @staticmethod
    def forward(context, x, constant):
        context.constant = constant
        return x.view_as(x) * constant

    @staticmethod
    def backward(context, grad):
        return grad.neg() * context.constant, None


class GRL(nn.Module):
    def __init__(self, alpha=1.0):
        super(GRL, self).__init__()
        self.alpha = alpha

    def forward(self, x):
        return GradientReversalLayer.apply(x, self.alpha)


class MultiTaskAutoEncoder(nn.Module):
    def __init__(self, option_list, seed=42):
        super(MultiTaskAutoEncoder, self).__init__()
        self.seed = seed
        self.batch_size = option_list['batch_size']
        self.feature_num = option_list['feature_num']
        self.latent_dim = option_list['latent_dim']
        self.celltype_num = option_list['celltype_num']
        self.num_epochs = option_list['epochs']
        self.lr = option_list['learning_rate']
        self.early_stop = option_list['early_stop']
        self.outdir = option_list['SaveResultsDir']
        self.pred_loss_type = option_list['pred_loss_type']
        self.loss_ref = option_list['loss_ref']
        assert self.loss_ref in ['pred_loss', 'total_loss'], "!! Invalid loss reference !!"

        self.rec_w = option_list['rec_w']
        self.pred_w = option_list['pred_w']
        self.disc_w = option_list['disc_w']

        self.losses = LossFunctions()

        cudnn.deterministic = True
        torch.cuda.manual_seed_all(self.seed)
        torch.manual_seed(self.seed)
        random.seed(self.seed)
    
    def MTAE_model(self):
        self.encoder = nn.Sequential(EncoderBlock(self.feature_num, 512, 0), 
                                     EncoderBlock(512, self.latent_dim, 0.2))
                                     
        self.decoder = nn.Sequential(DecoderBlock(self.latent_dim, 512, 0.2),
                                     DecoderBlock(512, self.feature_num, 0))

        self.predictor = nn.Sequential(EncoderBlock(self.latent_dim, 64, 0.2),
                                       nn.Linear(64, self.celltype_num),
                                       nn.Softmax(dim=1))
        
        self.discriminator = nn.Sequential(GRL(alpha=0.5),  # Gradient Reversal Layer
                                           EncoderBlock(self.latent_dim, 64, 0.2),
                                           nn.Linear(64, 1),
                                           nn.Sigmoid()) 

        model_da = nn.ModuleList([])
        model_da.append(self.encoder)
        model_da.append(self.decoder)
        model_da.append(self.predictor)
        model_da.append(self.discriminator)
        return model_da
    
    def train(self, source_data, target_data):
        # prepare model structure
        self.prepare_dataloader(source_data, target_data, self.batch_size)
        self.model_da = self.MTAE_model().cuda()

        # setup optimizer
        optimizer = torch.optim.Adam(self.model_da.parameters(), lr=self.lr)

        criterion_da = nn.BCELoss().cuda()
        source_label = torch.ones(self.batch_size).unsqueeze(1).cuda()   # source domain label as 1
        target_label = torch.zeros(self.batch_size).unsqueeze(1).cuda()  # target domain label as 0

        self.metric_logger = defaultdict(list) 
        best_loss = 1e10  
        update_flag = 0  
        for epoch in range(self.num_epochs):
            self.model_da.train()
            train_target_iterator = iter(self.train_target_loader)
            rec_loss_epoch, pred_loss_epoch, disc_loss_da_epoch = 0., 0., 0.
            all_preds = []
            all_labels = []
            for batch_idx, (source_x, source_y) in enumerate(self.train_source_loader):
                target_x = next(iter(self.train_target_loader))[0]   # NOTE: shuffle
                
                source_emb = self.encoder(source_x.cuda())
                target_emb = self.encoder(target_x.cuda())
                source_rec = self.decoder(source_emb)
                target_rec = self.decoder(target_emb)

                #### 1. reconstruction
                rec_loss = self.losses.reconstruction_loss(target_x.cuda(), target_rec, rec_type='mse') + self.losses.reconstruction_loss(source_x.cuda(), source_rec, rec_type='mse')
                rec_loss_epoch += rec_loss.data.item()

                #### 2. prediction
                source_pred = self.predictor(source_emb)
                if self.pred_loss_type == 'L1':
                    pred_loss = self.losses.L1_loss(source_pred, source_y.cuda())
                elif self.pred_loss_type == 'custom':
                    pred_loss = self.losses.summarize_loss(source_pred, source_y.cuda())
                else:
                    raise ValueError("Invalid prediction loss type.")
                pred_loss_epoch += pred_loss.data.item()

                #### 3. domain classification
                source_domain = self.discriminator(source_emb)
                target_domain = self.discriminator(target_emb)

                all_preds.extend(source_domain.cpu().detach().numpy().flatten())  # Source domain predictions
                all_preds.extend(target_domain.cpu().detach().numpy().flatten())  # Target domain predictions
                all_labels.extend([1]*source_domain.shape[0])  # Source domain labels
                all_labels.extend([0]*target_domain.shape[0])  # Target domain labels

                disc_loss_da = criterion_da(source_domain, source_label[0:source_domain.shape[0],]) + criterion_da(target_domain, target_label[0:target_domain.shape[0],])
                disc_loss_da_epoch += disc_loss_da.data.item()

                #### 4. total loss and optimization
                loss = self.rec_w * rec_loss + self.pred_w * pred_loss + self.disc_w * disc_loss_da

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            
            rec_loss_epoch = self.rec_w * rec_loss_epoch / len(self.train_source_loader)
            pred_loss_epoch = self.pred_w * pred_loss_epoch / len(self.train_source_loader)
            disc_loss_da_epoch = self.disc_w * disc_loss_da_epoch / len(self.train_source_loader)
            loss_all = rec_loss_epoch + pred_loss_epoch + disc_loss_da_epoch

            auc_score = roc_auc_score(all_labels, all_preds)

            self.metric_logger['rec_loss'].append(rec_loss_epoch)
            self.metric_logger['pred_loss'].append(pred_loss_epoch)
            self.metric_logger['disc_loss_DA'].append(disc_loss_da_epoch)
            self.metric_logger['total_loss'].append(loss_all)
            self.metric_logger['disc_auc'].append(auc_score)

            if epoch % 10 == 0:
                print(f"Epoch:{epoch}, Loss:{loss_all:.3f}, rec:{rec_loss_epoch:.3f}, pred:{pred_loss_epoch:.3f}, disc_da:{disc_loss_da_epoch:.3f}, disc_auc:{auc_score:.3f}")    

                # save best model
                #target_loss = self.metric_logger[self.loss_ref][-1]
                target_loss = rec_loss_epoch + pred_loss_epoch
                if target_loss < best_loss:
                    update_flag = 0
                    best_loss = target_loss
                    self.metric_logger['best_epoch'] = epoch
                    torch.save(self.model_da.state_dict(), os.path.join(self.outdir, 'best_model.pth'))
                else:
                    update_flag += 1
                    if update_flag == self.early_stop:
                        print("Early stopping at epoch %d" % (epoch+1))
                        break


    def load_checkpoint(self, model_path):
        self.model_da = self.MTAE_model().cuda()
        self.model_da.load_state_dict(torch.load(model_path))
        self.model_da.eval()

    def prediction(self, test_target_loader=None):
        if test_target_loader is None:
            test_target_loader = self.test_target_loader
            
        self.model_da.eval()
        preds, gt = None, None
        for batch_idx, (x, y) in enumerate(test_target_loader):
            logits = self.predictor(self.encoder(x.cuda())).detach().cpu().numpy()
            frac = y.detach().cpu().numpy()
            preds = logits if preds is None else np.concatenate((preds, logits), axis=0)
            gt = frac if gt is None else np.concatenate((gt, frac), axis=0)

        target_preds = pd.DataFrame(preds, columns=self.labels)
        ground_truth = pd.DataFrame(gt, columns=self.labels)  # random ratio is output if "real"
        return target_preds, ground_truth

    
    def prepare_dataloader(self, source_data, target_data, batch_size):
        ### Prepare data loader for training ###
        g = torch.Generator()
        g.manual_seed(42)

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
        self.target_data_y = np.random.rand(target_data.shape[0], self.celltype_num)

        te_data = torch.FloatTensor(self.target_data_x)
        te_labels = torch.FloatTensor(self.target_data_y)
        target_dataset = Data.TensorDataset(te_data, te_labels)
        self.train_target_loader = DataLoader(dataset=target_dataset, batch_size=batch_size, shuffle=True, worker_init_fn=seed_worker, generator=g)
        self.test_target_loader = Data.DataLoader(dataset=target_dataset, batch_size=batch_size, shuffle=False)


def preprocess(trainingdatapath, source='data6k', target='sdy67', n_samples=None, n_vtop=None):
    assert target in ['sdy67', 'GSE65133', 'donorA', 'donorC', 'data6k', 'data8k']
    pbmc = sc.read_h5ad(trainingdatapath)
    test = pbmc[pbmc.obs['ds']==target]

    if n_samples is not None:
        np.random.seed(42)
        idx = np.random.choice(8000, n_samples, replace=False)
        donorA = pbmc[pbmc.obs['ds']=='donorA'][idx]
        donorC = pbmc[pbmc.obs['ds']=='donorC'][idx]
        data6k = pbmc[pbmc.obs['ds']=='data6k'][idx]
        data8k = pbmc[pbmc.obs['ds']=='data8k'][idx]
    
    else:    
        donorA = pbmc[pbmc.obs['ds']=='donorA']
        donorC = pbmc[pbmc.obs['ds']=='donorC']
        data6k = pbmc[pbmc.obs['ds']=='data6k']
        data8k = pbmc[pbmc.obs['ds']=='data8k']

    if source == 'all':
        train = anndata.concat([donorA, donorC, data6k, data8k])
    else:
        if n_samples is not None:
            train = pbmc[pbmc.obs['ds']==source][idx]
        else:
            train = pbmc[pbmc.obs['ds']==source]

    train_y = train.obs.iloc[:,:-2]
    test_y = test.obs.iloc[:,:-2]

    
    if n_vtop is None:
        #### variance cut off
        label = test.X.var(axis=0) > 0.1  # FIXME: mild cut-off
    else:
        #### top 1000 highly variable genes
        label = np.argsort(-train.X.var(axis=0))[:n_vtop]
    
    train_data = train[:, label]
    train_data.X = np.log2(train_data.X + 1)
    test_data = test[:, label]
    test_data.X = np.log2(test_data.X + 1)

    print("Train data shape: ", train_data.X.shape)
    print("Test data shape: ", test_data.X.shape)

    return train_data, test_data, train_y, test_y

def seed_worker(worker_id):
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)

def set_random_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    random.seed(seed)
    np.random.seed(seed)
    cudnn.deterministic = True
    cudnn.benchmark = False

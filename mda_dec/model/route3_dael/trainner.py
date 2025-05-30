# -*- coding: utf-8 -*-
"""
Created on 2025-04-30 (Wed) 11:05:55

@author: I.Azuma
"""
# %%
BASE_DIR = '/workspace/mnt/cluster/HDD/azuma/TopicModel_Deconv'
import os
os.chdir(BASE_DIR)

import gc
import wandb
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

from scipy import sparse
from collections import defaultdict
from sklearn.preprocessing import MinMaxScaler

import torch

import sys
sys.path.append(BASE_DIR+'/github/GSTMDec/mda_dec')
from model.route3_dael import dael_da
from model.route3_dael import dael_utils

sys.path.append(BASE_DIR+'/github/deconv-utils')
from src import evaluation as ev

sys.path.append(BASE_DIR+'/github/wandb-util')  
from wandbutil import WandbLogger



class SimpleTrainer():
    def __init__(self, cfg):
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.cfg = cfg
        self.target_cells = cfg.common.target_cells
        self.noise_std_list = [0.0, 0.1, 0.5, 1.0]
        self.drop_prob_list = [0.0, 0.1, 0.2, 0.3]
        self.expert_selection = cfg.aedael.expert_selection

        self.data_preprocessing()
        print("--- Complete Data Preprocessing ---")
        
    
    def data_preprocessing(self):
        cfg = self.cfg

        total_list = cfg.common.source_list + cfg.common.target_list
        self.target_indices = [total_list.index(item) for item in cfg.common.target_list]
        print(f"Target indices: {self.target_indices}")

        train_data, gene_names = dael_utils.prep_daeldg(h5ad_path=cfg.paths.h5ad_path, 
                                                        source_list=total_list, 
                                                        n_samples=cfg.common.n_samples, 
                                                        n_vtop=cfg.common.n_vtop)
        self.train_data = train_data
        self.gene_names = gene_names

    def train_ae_dael(self):
        cfg = self.cfg
        # preparation of option list
        option_list = defaultdict(list)
        for key, value in vars(cfg.aedael).items():
            option_list[key] = value
        option_list['feature_num'] = self.train_data.shape[1]
        option_list['celltype_num'] = len(self.target_cells)
        option_list['n_domain'] = len(cfg.common.source_list)

        source_data = self.train_data[self.train_data.obs['ds'].isin(cfg.common.source_list)]
        target_data = self.train_data[self.train_data.obs['ds'].isin(cfg.common.target_list)]

        model = dael_da.AE_DAEL(option_list, seed=cfg.aedael.seed).to(self.device)
        self.label_loader = model.multi_aug_dataloaders(source_data, batch_size=model.batch_size, weak_noise=model.weak_noise, strong_noise=model.strong_noise, target_cells=self.target_cells)
        self.unlabel_loader = model.multi_aug_dataloaders(target_data, batch_size=model.batch_size, weak_noise=model.weak_noise, strong_noise=model.strong_noise, target_cells=self.target_cells)
        print("--- Complete Build Data Loader ---")

        optimizer = torch.optim.Adam(model.parameters(), lr=cfg.aedael.learning_rate)

        # WandB logger settings
        logger = WandbLogger(
            entity=cfg.wandb.entity,  
            project=cfg.wandb.project,  
            group=cfg.wandb.group, 
            name=cfg.wandb.name,
            config=option_list,
        )


        best_loss = 1e10  
        for epoch in range(cfg.aedael.epochs):
            model.train()
            rec_loss_epoch, loss_x_epoch, loss_cr_epoch, loss_u_epoch = 0, 0, 0, 0
            
            for batch_idx, (data_wx_l, data_sx_l, data_y_l, domain_l) in enumerate(self.label_loader):
                # --- 1. Reconstruction of Target ---
                data_wx_u, data_sx_u, data_y_u, domain_u = next(iter(self.unlabel_loader))
                data_wx_u = data_wx_u.to(self.device)
                data_sx_u = data_sx_u.to(self.device)
                rec_wx_u, latent_w_u, domain_w_u = model(data_wx_u)
                rec_sx_u, latent_s_u, domain_s_u = model(data_sx_u)

                source_mse = model.losses.rec_loss(data_wx_u, rec_wx_u) + model.losses.rec_loss(data_sx_u, rec_sx_u)

                # --- 2. Reconstruction of Sources ---
                data_wx_l = data_wx_l.to(self.device)
                data_sx_l = data_sx_l.to(self.device)

                rec_wx_l, latent_w_l, domain_w_l = model(data_wx_l)  # latent_w_l: (128, 256)
                rec_sx_l, latent_s_l, domain_s_l = model(data_sx_l)

                target_mse = model.losses.rec_loss(data_wx_l, rec_wx_l) + model.losses.rec_loss(data_sx_l, rec_sx_l)
                rec_mse = source_mse + target_mse  # FIXME: unbalanced

                # --- Generate pseudo label for unlabeled (target) data ---
                with torch.no_grad():
                    if self.expert_selection == 'conformal':
                        u_vars = []
                        for j in range(model.n_domain):
                            domain_j = torch.full((latent_w_u.size(0),), j, device=latent_w_u.device)
                            aug_pool = []
                            for noise_v in self.noise_std_list:
                                for drop_v in self.drop_prob_list:
                                    # augmentation (adding noise x random masking)
                                    feat_u_aug = dael_utils.add_noise(latent_w_u, noise_std=noise_v)
                                    feat_u_aug = dael_utils.random_masking(feat_u_aug, mask_prob=drop_v)
                                    pred_j = model.E(domain_j, feat_u_aug)  # (batch_size, num_classes)
                                    aug_pool.append(pred_j)
                            aug_stacked = torch.stack(aug_pool, dim=-1)  # (B, n_class, n_augmentation)
                            # calc var
                            var_mat = aug_stacked.std(dim=-1)  # (B, n_class)
                            #var_sample = var_mat.mean(dim=1)  # (B,)
                            var_sample = var_mat.max(dim=1).values  # (B,)
                            u_vars.append(var_sample)
                        #print(u_vars)
                        stacked = torch.stack(u_vars, dim=1)  # (B, n_domain)
                        min_idx = stacked.min(dim=1).indices  # (B, )  e.g. [3, 1, 3, 0, 3, 3, 1, 2, 3, 1, 1, 1]
                        pseudo_prop = model.E(min_idx, latent_w_u)  # (batch_size, num_classes)
                    else:
                        u_preds = []
                        for j in range(model.n_domain):
                            # Pass all the feat (weak augumented) to the jth Expert
                            domain_j = torch.full((latent_w_u.size(0),), j, device=latent_w_u.device)
                            pred_j = model.E(domain_j, latent_w_u)  # (batch_size, num_classes)
                            u_preds.append(pred_j)
                        stacked = torch.stack(u_preds, dim=-1)  # (B, n_class, n_domain)
                        if self.expert_selection == 'mean':
                            pseudo_prop = stacked.mean(dim=-1)
                        elif self.expert_selection == 'max':
                            max_idx = stacked.max(dim=1).values.max(dim=1).indices
                            max_idx = max_idx.view(-1, 1)
                            max_idx = max_idx.expand(-1, stacked.size(1))
                            pseudo_prop = stacked.gather(dim=-1, index=max_idx.unsqueeze(-1)).squeeze(-1)
                        else:
                            raise ValueError(f"Unknown expert selection method: {self.expert_selection}")

                # --- 2.Expert prediction (weak augmentation) ---"
                domain_l = domain_l.to(self.device)
                data_y_l = data_y_l.to(self.device)
                pred = model.E(domain_l, latent_w_l)  # (batch_size, num_classes)
                loss_x = ((data_y_l - pred) ** 2).mean()

                expert_pred = pred.detach()

                # --- 3. Consistency regularization (strong augmentation) ---"
                loss_cr = 0
                cr_preds = []
                for j in range(model.n_domain):
                    mask = (domain_l != j)  # (batch_size,) bool tensor
                    mask_counts = mask.sum()  # number of samples not in domain j
                    # Pass all the feat2 to the jth Expert
                    domain_j = torch.full_like(domain_l, j)
                    pred_j = model.E(domain_j, latent_s_l)  # (batch_size, num_classes)
                    pred_j = pred_j * mask.unsqueeze(1).float()
                    cr_preds.append(pred_j)  # use masked area
                
                stacked = torch.stack(cr_preds, dim=-1)  # (stacked_size, num_classes, num_domains)
                mask = (stacked != 0)
                sum_valid = (stacked * mask).sum(dim=-1)
                count_valid = mask.sum(dim=-1)
                assert (count_valid == model.n_domain-1).all(), "Not all elements are K-1!"
                cr_preds_m = sum_valid / (count_valid + 1e-8)  # (batch, num_classes)
                
                loss_cr = ((cr_preds_m - expert_pred) ** 2).mean()

                # --- 4. Unsupervised loss ---"
                u_preds = []
                for j in range(model.n_domain):
                    # Pass all the feat (strong augumented) to the jth Expert
                    domain_j = torch.full((latent_s_u.size(0),), j, device=latent_s_u.device)
                    pred_j = model.E(domain_j, latent_s_u)  # (batch_size, num_classes)
                    u_preds.append(pred_j)
                stacked = torch.stack(u_preds, dim=-1)  # (B, n_class, n_domain)
                pred_u = stacked.mean(dim=-1)

                loss_u = ((pseudo_prop - pred_u) ** 2).mean()

                # --- Backprop ---
                loss = 0
                loss += rec_mse * cfg.aedael.weight_rec
                loss += loss_x * cfg.aedael.weight_x
                loss += loss_cr * cfg.aedael.weight_cr
                loss += loss_u * cfg.aedael.weight_u

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                
                rec_loss_epoch += rec_mse.item() * cfg.aedael.weight_rec
                loss_x_epoch += loss_x.item() * cfg.aedael.weight_x
                loss_cr_epoch += loss_cr.item() * cfg.aedael.weight_cr
                loss_u_epoch += loss_u.item() * cfg.aedael.weight_u
            
            # save best model and early stopping
            target_loss = rec_loss_epoch + loss_x_epoch + loss_cr_epoch + loss_u_epoch
            if target_loss < best_loss:
                update_flag = 0
                best_loss = target_loss
                torch.save(model.state_dict(), os.path.join(cfg.paths.aedael_model_path, f'aedael_best.pth'))
            else:
                update_flag += 1
                if update_flag == option_list['early_stop']:
                    print("Early stopping at epoch %d" % (epoch+1))
                    break

            # inference
            summary_df = self.target_inference(model, target_data)

            # logging
            logger(
                epoch=epoch,
                loss=target_loss,
                loss_rec=rec_loss_epoch,
                loss_x=loss_x_epoch,
                loss_cr=loss_cr_epoch,
                loss_u=loss_u_epoch,
                R=summary_df.loc['mean']['R'],
                CCC=summary_df.loc['mean']['CCC'],
                MAE=summary_df.loc['mean']['MAE'],
            )
            
            if (epoch) % 10 == 0:
                print(f"Epoch {epoch}: Loss: {target_loss:.4f}, Loss_rec: {rec_loss_epoch:.4f}, Loss_x: {loss_x_epoch:.4f}, Loss_cr: {loss_cr_epoch:.4f}, Loss_u: {loss_u_epoch:.4f}")
            
            gc.collect()
            
    def target_inference(self, model, target_data):
        model.eval()
        input_x = torch.tensor(target_data.X.toarray(), dtype=torch.float32).to(self.device)
        data_y = target_data.obs[self.target_cells].values
        rec, latent, d = model(input_x)
        u_preds = []
        for j in range(model.n_domain):
            # Pass all the feat (weak augumented) to the jth Expert
            domain_j = torch.full((latent.shape[0],), j, device=self.device)
            pred_j = model.E(domain_j, latent)  # (batch_size, num_classes)
            u_preds.append(pred_j)
        stacked = torch.stack(u_preds, dim=-1)  # (B, n_class, n_domain)
        p_k_t = stacked.mean(dim=-1)

        dec_df = pd.DataFrame(p_k_t.cpu().detach().numpy(), columns=self.target_cells)
        y_df = pd.DataFrame(data_y, columns=self.target_cells)

        # Evaluation
        dec_name_list = [["Monocytes"],["Unknown"],["Bcells"],["CD4Tcells"],["CD8Tcells"],["NK"]]
        val_name_list = [["Monocytes"],["Unknown"],["Bcells"],["CD4Tcells"],["CD8Tcells"],["NK"]]

        res = ev.eval_deconv(dec_name_list=dec_name_list, val_name_list=val_name_list, deconv_df=dec_df, y_df=y_df, do_plot=False)

        # summarize
        r_list = []
        mae_list = []
        ccc_list = []
        for i in range(len(dec_name_list)):
            tmp_res = res[i][0]
            r, mae, ccc = tmp_res['R'], tmp_res['MAE'], tmp_res['CCC']
            r_list.append(r)
            mae_list.append(mae)
            ccc_list.append(ccc)
        summary_df = pd.DataFrame({'R':r_list, 'CCC':ccc_list, 'MAE':mae_list})
        summary_df.index = [t[0] for t in val_name_list]
        summary_df.loc['mean'] = summary_df.mean()

        return summary_df

    
    def overall_inference(self, domains, target_domain=[], plot_target=True):
        cfg = self.cfg

        # preparation of option list
        option_list = defaultdict(list)
        for key, value in vars(cfg.dael).items():
            option_list[key] = value
        
        assert self.feats0.shape[1] == self.feats1.shape[1] == self.feats2.shape[1], "Feature dimension mismatch!"
        option_list['feature_num'] = self.feats0.shape[1]
        option_list['celltype_num'] = len(self.target_cells)
        option_list['n_domain'] = len(cfg.common.source_list)

        self.expert_selection = cfg.dael.expert_selection
        self.weight_u = cfg.dael.weight_u

        if target_domain == []:
            target_domain = self.target_indices
        target_idx = torch.isin(domains, torch.tensor(target_domain, device=domains.device))
        s_domains = domains[~target_idx]
        t_domains = domains[target_idx]

        s_feats = self.feats0[~target_idx,:]
        t_feats = self.feats0[target_idx,:]

        # load best model
        dael_model = dael_da.DAEL(option_list, seed=42).to(self.device)
        dael_model.load_state_dict(torch.load(os.path.join(cfg.paths.dael_model_path, f'dael_da_best.pth')))

        # inference source
        dael_model.eval()
        p_k_s = dael_model.E(s_domains, s_feats)

        # inference target
        dael_model.eval()
        u_preds = []
        for j in range(dael_model.n_domain):
            # Pass all the feat (weak augumented) to the jth Expert
            domain_j = torch.full((t_feats.shape[0],), j, device=t_feats.device)
            pred_j = dael_model.E(domain_j, t_feats)  # (batch_size, num_classes)
            u_preds.append(pred_j)
        stacked = torch.stack(u_preds, dim=-1)  # (B, n_class, n_domain)
        p_k_t = stacked.mean(dim=-1)

        # concat
        p_k = torch.cat((p_k_s, p_k_t), dim=0)


        dec_df = pd.DataFrame(p_k.cpu().detach().numpy(), columns=self.target_cells)
        y_df = pd.DataFrame(self.data_y.cpu().detach().numpy(), columns=self.target_cells)
        dec_name_list = [["Monocytes"],["Unknown"],["Bcells"],["CD4Tcells"],["CD8Tcells"],["NK"]]
        val_name_list = [["Monocytes"],["Unknown"],["Bcells"],["CD4Tcells"],["CD8Tcells"],["NK"]]

        for d in range(dael_model.n_domain+1):
            # select the domain index
            d_idx = (domains.cpu().detach().numpy() == d).nonzero()[0]
            d_dec_df = dec_df.iloc[d_idx, :]
            d_y_df = y_df.iloc[d_idx, :]

            do_plot = plot_target and d in target_domain
            res = ev.eval_deconv(dec_name_list=dec_name_list, val_name_list=val_name_list,
                     deconv_df=d_dec_df, y_df=d_y_df, do_plot=do_plot)
                
            # summarize
            r_list = []
            mae_list = []
            ccc_list = []
            for i in range(len(dec_name_list)):
                tmp_res = res[i][0]
                r, mae, ccc = tmp_res['R'], tmp_res['MAE'], tmp_res['CCC']
                r_list.append(r)
                mae_list.append(mae)
                ccc_list.append(ccc)
            summary_df = pd.DataFrame({'R':r_list, 'CCC':ccc_list, 'MAE':mae_list})
            summary_df.index = [t[0] for t in val_name_list]
            summary_df.loc['mean'] = summary_df.mean()

            display(summary_df)

import torch
import timm
import numpy as np
import os
import math
from einops import repeat, rearrange
from einops.layers.torch import Rearrange
from torchvision.transforms import ToTensor, Compose, Normalize
from timm.models.layers import trunc_normal_
from timm.models.vision_transformer import Block
import anndata as ad
from sklearn.model_selection import train_test_split
import time
import random
from tqdm import tqdm
import matplotlib.pyplot as plt
from torch.nn.parallel import DataParallel
import pandas as pd
from torch.utils.data import DataLoader
import warnings
warnings.filterwarnings("ignore")

class Config(object):
    """para"""
    def __init__(self):
        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')  
        self.dropout = 0.1                                    
        self.num_classes = 14                                             
        self.batch_size = 128                                       
        self.lr = 5e-4                      
        self.encoder_layer = 6
        self.encoder_head = 2
        self.decoder_layer = 4
        self.decoder_head = 2
        self.mask_ratio = 0.15
        
        self.RNA_tokens = 4000 ##RNA number
        self.RNA_component = 400 ##channel        
        self.emb_RNA = 10
        
        self.mask_ratio1 = 0.15 
        
        
        self.ADT_tokens = 14 ##proteins umber
        self.ADT_component =14 ##channel
        self.emb_ADT = 1 
        
        self.emb_dim = 128       
        self.total_epoch = 500
        self.warmup_epoch = 10

config = Config()
##preprocessing
def random_indexes(size : int):
    forward_indexes = np.arange(size)
    np.random.shuffle(forward_indexes)
    backward_indexes = np.argsort(forward_indexes)
    return forward_indexes, backward_indexes

def take_indexes(sequences, indexes):
    return torch.gather(sequences, 0, repeat(indexes, 't b -> t b c', c=sequences.shape[-1]))

class PatchShuffle(torch.nn.Module):
    def __init__(self, ratio):
        super().__init__()
        self.ratio = ratio

    def forward(self, patches : torch.Tensor):
        T, B, C = patches.shape
        remain_T = int(T * (1 - self.ratio))

        indexes = [random_indexes(T) for _ in range(B)]
        forward_indexes = torch.as_tensor(np.stack([i[0] for i in indexes], axis=-1), dtype=torch.long).to(patches.device)
        backward_indexes = torch.as_tensor(np.stack([i[1] for i in indexes], axis=-1), dtype=torch.long).to(patches.device)

        patches = take_indexes(patches, forward_indexes)
        patches = patches[:remain_T]

        return patches, forward_indexes, backward_indexes

## RNA encoder
class RNA_Encoder(torch.nn.Module):
    def __init__(self,emb_dim=64,emb_RNA=10,RNA_component=400,RNA_tokens=4000, encoder_head=4,encoder_layer=6,mask_ratio=0.1
                 )-> None:
        super().__init__()
        self.tokens = torch.nn.Sequential(torch.nn.Linear(in_features = RNA_tokens, out_features = RNA_tokens))
        self.embedding = torch.nn.Sequential(torch.nn.Linear(in_features = emb_RNA, out_features = emb_dim))
        self.cls_token = torch.nn.Parameter(torch.zeros(1, 1,emb_dim))
        self.pos_embedding = torch.nn.Parameter(torch.zeros((1, RNA_component ,emb_dim)))##
        self.shuffle = PatchShuffle(mask_ratio)
        self.transformer = torch.nn.Sequential(*[Block(emb_dim, encoder_head,attn_drop=0.1 ,proj_drop=0.1) for _ in range(encoder_layer)])
        self.layer_norm = torch.nn.LayerNorm(emb_dim)

        self.init_weight()

    def init_weight(self):
        trunc_normal_(self.cls_token, std=.02)
        trunc_normal_(self.pos_embedding, std=.02)

    def forward(self, patches):
       
        patches = self.tokens(patches)
        patches = patches.view(patches.size(0), config.RNA_component, config.emb_RNA)
        patches = self.embedding(patches)
        patches = patches + self.pos_embedding

        patches = rearrange(patches, 'b c s -> c b s')        
        patches, forward_indexes, backward_indexes = self.shuffle(patches)    
        patches = torch.cat([self.cls_token.expand(-1,patches.shape[1],-1), patches], dim=0)
        patches = rearrange(patches, 't b c -> b t c')

        features = self.layer_norm(self.transformer(patches))
        features = rearrange(features, 'b t c -> t b c')

        return features, backward_indexes

## ADT encoder
class ADT_Encoder(torch.nn.Module):
    def __init__(self,emb_dim=64,emb_ADT=10,ADT_component=11,ADT_tokens=110, encoder_head=4,encoder_layer=6,mask_ratio1=0.1
                 )-> None:
        super().__init__()
        self.tokens = torch.nn.Sequential(torch.nn.Linear(in_features = ADT_tokens, out_features = ADT_tokens))
        self.embedding = torch.nn.Sequential(torch.nn.Linear(in_features = emb_ADT, out_features = emb_dim))
        self.cls_token = torch.nn.Parameter(torch.zeros(1, 1,emb_dim))
        self.pos_embedding = torch.nn.Parameter(torch.zeros((1, ADT_component ,emb_dim)))##
        self.shuffle = PatchShuffle(mask_ratio1)
        self.transformer = torch.nn.Sequential(*[Block(emb_dim, encoder_head,attn_drop=0.1 ,proj_drop=0.1) for _ in range(encoder_layer)])
        self.layer_norm = torch.nn.LayerNorm(emb_dim)

        self.init_weight()

    def init_weight(self):
        trunc_normal_(self.cls_token, std=.02)
        trunc_normal_(self.pos_embedding, std=.02)

    def forward(self, patches):

        patches = self.tokens(patches)

        patches = patches.view(patches.size(0), config.ADT_component, config.emb_ADT)
        patches = self.embedding(patches)
        patches = patches + self.pos_embedding

        patches = rearrange(patches, 'b c s -> c b s')
        patches, forward_indexes, backward_indexes = self.shuffle(patches)    
        patches = torch.cat([self.cls_token.expand(-1,patches.shape[1],-1), patches], dim=0)

        patches = rearrange(patches, 't b c -> b t c')
        features = self.layer_norm(self.transformer(patches))
        features = rearrange(features, 'b t c -> t b c')

        return features, backward_indexes

##RNA decoder
class RNA_Decoder(torch.nn.Module):
    def __init__(self,emb_dim=64,emb_RNA=10,RNA_component=400,RNA_tokens=4000,decoder_head=4,decoder_layer=2
                 )-> None:
        super().__init__()

        self.mask_token = torch.nn.Parameter(torch.zeros(1, 1, emb_dim))
        self.pos_embedding = torch.nn.Parameter(torch.zeros((RNA_component + 1, 1, emb_dim)))##
        self.transformer = torch.nn.Sequential(*[Block(emb_dim, decoder_head,attn_drop=0.1 ,proj_drop=0.1) for _ in range(decoder_layer)]) 


        self.decoding = torch.nn.Sequential(torch.nn.Linear(emb_dim, emb_RNA))
        self.init_weight()
        
    def init_weight(self):
        trunc_normal_(self.mask_token, std=.02)
        trunc_normal_(self.pos_embedding, std=.02)

    def forward(self, features, backward_indexes):
        T = features.shape[0]

        backward_indexes = torch.cat([torch.zeros(1, backward_indexes.shape[1]).to(backward_indexes), backward_indexes + 1], dim=0)
        features = torch.cat([features, self.mask_token.expand(backward_indexes.shape[0] - features.shape[0], features.shape[1], -1)], dim=0)
        features = take_indexes(features, backward_indexes)

        features = features + self.pos_embedding
        features = rearrange(features, 't b c -> b t c')
        features = self.transformer(features)
        features = rearrange(features, 'b t c -> t b c')

        all_cls = []
        first_element = features[0]
        all_cls.append(first_element)
        
        patches = features[1:] # remove global feature
        patches = self.decoding(patches)##128-10
        mask = torch.zeros_like(patches)

        mask[T-1:] = 1
        mask = take_indexes(mask, backward_indexes[1:] - 1)

        patches = rearrange(patches, 't b c -> b t c')
        mask = rearrange(mask, 't b c -> b t c')
 
        patches = patches.reshape(patches.size(0),1,-1)
        mask = mask.reshape(mask.size(0),1,-1)

        return patches, mask, all_cls

##ADT_Decoder
class ADT_Decoder(torch.nn.Module):
    def __init__(self,emb_dim=64,emb_ADT=10,ADT_component=11,ADT_tokens=110,decoder_head=4,decoder_layer=2
                 )-> None:
        super().__init__()

        self.mask_token = torch.nn.Parameter(torch.zeros(1, 1, emb_dim))
        self.pos_embedding = torch.nn.Parameter(torch.zeros((ADT_component + 1, 1, emb_dim)))##
        self.transformer = torch.nn.Sequential(*[Block(emb_dim, decoder_head,attn_drop=0.1 ,proj_drop=0.1) for _ in range(decoder_layer)])        

        self.decoding = torch.nn.Sequential(torch.nn.Linear(emb_dim, emb_ADT))
        self.init_weight()
        
    def init_weight(self):
        trunc_normal_(self.mask_token, std=.02)
        trunc_normal_(self.pos_embedding, std=.02)

    def forward(self, features, backward_indexes):
        T = features.shape[0]

        backward_indexes = torch.cat([torch.zeros(1, backward_indexes.shape[1]).to(backward_indexes), backward_indexes + 1], dim=0)

        features = torch.cat([features, self.mask_token.expand(backward_indexes.shape[0] - features.shape[0], features.shape[1], -1)], dim=0)
        features = take_indexes(features, backward_indexes)
        features = features + self.pos_embedding
        features = rearrange(features, 't b c -> b t c')
        features = self.transformer(features)
        features = rearrange(features, 'b t c -> t b c')

        all_cls = []
        first_element = features[0]
        all_cls.append(first_element)
        
        patches = features[1:] # remove global feature
        patches = self.decoding(patches)##128-10
        
        
        mask = torch.zeros_like(patches)
        mask[T-1:] = 1
        mask = take_indexes(mask, backward_indexes[1:] - 1)

        patches = rearrange(patches, 't b c -> b t c')
        mask = rearrange(mask, 't b c -> b t c')

        patches = patches.reshape(patches.size(0),1,-1)
        mask = mask.reshape(mask.size(0),1,-1)
        
        return patches, mask, all_cls

#cross attention
class CrossAttention(torch.nn.Module):
    def __init__(self, emb_dim=64)-> None:
        super(CrossAttention, self).__init__()
        self.query_linear = torch.nn.Linear(emb_dim, emb_dim)
        self.key_linear = torch.nn.Linear(emb_dim, emb_dim)
        self.value_linear = torch.nn.Linear(emb_dim, emb_dim)
        self.scale_factor = 1.0 / (emb_dim ** 0.5)
        # Layer Normalization
        self.layer_norm = torch.nn.LayerNorm(emb_dim)
    def forward(self, query, key, value):
        query_proj = self.query_linear(query)  # 
        key_proj = self.key_linear(key)  # 
        value_proj = self.value_linear(value)  # 

        attention_scores = torch.matmul(query_proj, key_proj.transpose(-2, -1))#T)  # 
        attention_scores = attention_scores * self.scale_factor # 
        attention_weights = torch.softmax(attention_scores, dim=-1)  # 

        attended_values = torch.matmul(attention_weights, value_proj)  # 
        attended_values = self.layer_norm(attended_values)
        return attended_values


        
class Omics_attention(torch.nn.Module):
    def __init__(self,config
                 ):
        super().__init__()

        self.RNA_Encoder = RNA_Encoder(config.emb_dim,config.emb_RNA,config.RNA_component,config.RNA_tokens,config.encoder_head,config.encoder_layer,config.mask_ratio)
        self.ADT_Encoder = ADT_Encoder(config.emb_dim,config.emb_ADT,config.ADT_component,config.ADT_tokens,config.encoder_head,config.encoder_layer,config.mask_ratio1)
       
 ##cross
        self.RNA_in_ADT_Att = CrossAttention(config.emb_dim)
        self.ADT_in_RNA_Att = CrossAttention(config.emb_dim)        
        
        self.RNA_Decoder = RNA_Decoder(config.emb_dim,config.emb_RNA,config.RNA_component,config.RNA_tokens,config.decoder_head,config.decoder_layer)
        self.ADT_Decoder = ADT_Decoder(config.emb_dim,config.emb_ADT,config.ADT_component,config.ADT_tokens,config.decoder_head,config.decoder_layer)

        
    def forward(self, patches1,patches2):


        omics_encoder_feature1, backward_indexes1 = self.RNA_Encoder(patches1)
        omics_encoder_feature2, backward_indexes2 = self.ADT_Encoder(patches2)


        omics_feature1 = rearrange(omics_encoder_feature1.clone(), 't b c -> b t c')
        omics_feature1_cls = omics_feature1[:,0, :].unsqueeze(1).clone() #cls
        omics_feature2 = rearrange(omics_encoder_feature2.clone(), 't b c -> b t c')
        omics_feature2_cls = omics_feature2[:,0, :].unsqueeze(1).clone() #cls

        x2 = self.ADT_in_RNA_Att(omics_feature1_cls, omics_feature2_cls, omics_feature2_cls)## 
        x1 = self.RNA_in_ADT_Att(omics_feature2_cls, omics_feature1_cls, omics_feature1_cls)##      
        omics_feature1[:,0, :] = x2.clone().squeeze(1)
        omics_feature2[:,0, :] = x1.clone().squeeze(1)

        
        
        omics_feature1 = rearrange(omics_feature1.clone(), 'b t c -> t b c')
        omics_feature2 = rearrange(omics_feature2.clone(), 'b t c -> t b c')

        omics_feature1 += omics_encoder_feature1
        omics_feature2 += omics_encoder_feature2

        omics_patches1, mask1, all_cls1 = self.RNA_Decoder(omics_feature1,backward_indexes1) 
        omics_patches2, mask2, all_cls2 = self.ADT_Decoder(omics_feature2,backward_indexes2)
       
        return omics_patches1,omics_patches2, mask1,mask2, all_cls1, all_cls2#,final_result







############Loading Dataset
RNA = torch.load('../dataset/CITE-seq/malt_10k_rna_rpkm.pth')
ADT = torch.load('../dataset/CITE-seq/malt_10k_prot_clred.pth')
RNA.shape, ADT.shape

from dataloader import *
##RNA
train_dataset,val_dataset = train_test_split(RNA,test_size=0.25, random_state=42)
train_dataset = train_dataset.to(torch.float).to(config.device)
val_dataset = val_dataset.to(torch.float).to(config.device)
M_train = len(train_dataset)
M_val = len(val_dataset)
##ADT
train_dataset1,val_dataset1 = train_test_split(ADT, test_size=0.25, random_state=42)
train_dataset1 = train_dataset1.to(torch.float).to(config.device)
val_dataset1 = val_dataset1.to(torch.float).to(config.device)
M_train1 = len(train_dataset1)
M_val1 = len(val_dataset1)

multi_modal_trian_dataset = MultiModalDataset(train_dataset, train_dataset1)
multi_modal_test_dataset = MultiModalDataset(val_dataset, val_dataset1)
train_dataloader = torch.utils.data.DataLoader(multi_modal_trian_dataset, 128, shuffle=True,num_workers=0)
val_dataloader = torch.utils.data.DataLoader(multi_modal_test_dataset, 128, shuffle=False,num_workers=0)

############################training
early_stopping_patience = 5  
best_val_loss = float('inf')  
no_improvement_count = 0
weight_a = 0.7
weight_b = 0.3 

model = Omics_attention(config).to(config.device)
model = DataParallel(model)
if __name__ == '__main__':
    
        batch_size = config.batch_size
        load_batch_size = 128
        assert batch_size % load_batch_size == 0
        steps_per_update = batch_size // load_batch_size

        optim = torch.optim.AdamW(model.parameters(), lr=config.lr * config.batch_size / 256, betas=(0.9, 0.999), weight_decay=1e-4)
        lr_func = lambda epoch: min((epoch + 1) / (config.warmup_epoch + 1e-8), 0.5 * (math.cos(epoch / config.total_epoch * math.pi) + 1))
        lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optim, lr_lambda=lr_func, verbose=True)

        best_val_acc = 0
        step_count = 0
        optim.zero_grad()
        
        train_losses_list = []
        val_losses_list = []
        for e in range(config.total_epoch):
            model.train()
            train_losses = []
            for tk in tqdm(iter(train_dataloader)):
               
                step_count += 1

                
                RNA_patches,ADT_patches, mask1,mask2,all_RNAcls, all_ADTcls = model(tk['mod1'], tk['mod2'])
                
                loss_a = torch.mean((RNA_patches - tk['mod1']) ** 2 * mask1) / config.mask_ratio
                loss_b = torch.mean((ADT_patches - tk['mod2']) ** 2 * mask2) / config.mask_ratio1
                train_loss = weight_a * loss_a + weight_b * loss_b
                train_loss.backward()
                if step_count % steps_per_update == 0:
                    optim.step()
                    optim.zero_grad()
                train_losses.append(train_loss.item())
            lr_scheduler.step()
            avg_train_loss = sum(train_losses) / len(train_losses)
            train_losses_list.append(avg_train_loss)
            print(f'In epoch {e}, average training loss is {avg_train_loss}.')

            model.eval()
            with torch.no_grad():
                val_losses = []
                for td in tqdm(iter(val_dataloader)):
                    RNA_patches_val,ADT_patches_val, mask1_val,mask2_val, all_RNAcls_val, all_ADTcls_val = model(td['mod1'], td['mod2'])
                    
                    loss_c = torch.mean((RNA_patches_val - td['mod1']) ** 2 * mask1_val) / config.mask_ratio
                    loss_d = torch.mean((ADT_patches_val - td['mod2']) ** 2 * mask2_val) / config.mask_ratio1           
                    val_loss = weight_a * loss_c + weight_b * loss_d
                    val_losses.append(val_loss.item())
                avg_val_loss = sum(val_losses) / len(val_losses)
                val_losses_list.append(avg_val_loss)
                print(f'In epoch {e}, average validation loss is {avg_val_loss}.')  

        # 
            if avg_val_loss < best_val_loss:
                best_val_loss = avg_val_loss
                no_improvement_count = 0  #
                print(f'Saving best model with loss {best_val_loss} at epoch {e}!')

                #save best paras
                #torch.save(model.state_dict(), f'{Your Path}/scMMAE_{dataset}_pretrain_{e}epoch_best_model.pth')  # 
            else:
                no_improvement_count += 1

            # np.save('trloss.npy', train_losses[0])
            # np.save('valloss.npy', val_losses[0])
            if no_improvement_count >= early_stopping_patience:
                print(f'No improvement in validation loss for {early_stopping_patience} epochs. Early stopping!')
                break  # 







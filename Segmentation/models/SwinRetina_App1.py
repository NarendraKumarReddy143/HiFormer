import torch
import numpy as np
import torch.nn as nn
import torch.utils.checkpoint as checkpoint
import torchvision
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from utils import *
from einops import rearrange, repeat
from einops.layers.torch import Rearrange
from torch.nn import functional as F


class Attention(nn.Module):
    def __init__(self, dim, heads = 8, dim_head = 64, dropout = 0.):
        super().__init__()
        inner_dim = dim_head *  heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5

        self.attend = nn.Softmax(dim = -1)
        self.dropout = nn.Dropout(dropout)

        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias = False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()

    def forward(self, x):
        qkv = self.to_qkv(x).chunk(3, dim = -1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = self.heads), qkv)

        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale

        attn = self.attend(dots)
        attn = self.dropout(attn)

        out = torch.matmul(attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)


class SwinTransformer(nn.Module):
    def __init__(self, img_size=224, patch_size=4, in_chans=3, num_classes=1000,
                 embed_dim=96, depths=[2, 2, 6, 2], num_heads=[3, 6, 12, 24],
                 window_size=7, mlp_ratio=4., qkv_bias=True, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.1,
                 norm_layer=nn.LayerNorm, ape=False, patch_norm=True,
                 use_checkpoint=False, **kwargs):
        
        super().__init__()
        
        patches_resolution = [img_size // patch_size, img_size // patch_size]
        num_patches = patches_resolution[0] * patches_resolution[1]
        
        self.num_classes = num_classes
        self.num_layers = len(depths)
        self.embed_dim = embed_dim
        self.ape = ape
        self.patch_norm = patch_norm
        self.num_features = int(embed_dim * 2 ** (self.num_layers - 1))
        self.mlp_ratio = mlp_ratio


        if self.ape:
            self.absolute_pos_embed = nn.Parameter(torch.zeros(1, num_patches, embed_dim))
            trunc_normal_(self.absolute_pos_embed, std=.02)

        self.pos_drop = nn.Dropout(p=drop_rate)

        # stochastic depth
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule

        # build layers
        self.layers = nn.ModuleList()
        for i_layer in range(self.num_layers):
            layer = BasicLayer(dim=int(embed_dim * 2 ** i_layer),
                               input_resolution=(patches_resolution[0] // (2 ** i_layer),
                                                 patches_resolution[1] // (2 ** i_layer)),
                               depth=depths[i_layer],
                               num_heads=num_heads[i_layer],
                               window_size=window_size,
                               mlp_ratio=self.mlp_ratio,
                               qkv_bias=qkv_bias, qk_scale=qk_scale,
                               drop=drop_rate, attn_drop=attn_drop_rate,
                               drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                               norm_layer=norm_layer,
                               downsample= None, 
                               use_checkpoint=use_checkpoint)
            self.layers.append(layer)

        self.norm = norm_layer(self.num_features)
        self.avgpool = nn.AdaptiveAvgPool1d(1)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'absolute_pos_embed'}

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table'}

    def forward_features(self, x):
        if self.ape:
            x = x + self.absolute_pos_embed
        x = self.pos_drop(x)

        for layer in self.layers:
            x = layer(x)
            print(x.shape)

        x = self.norm(x)  # B L C
        x = self.avgpool(x.transpose(1, 2))  # B C 1
        x = torch.flatten(x, 1)
        return x

    def forward(self, x):
        x = self.forward_features(x)
        return x


class PyramidFeatures(nn.Module):
    def __init__(self, config, img_size = 224, in_channels = 3):
        super().__init__()
        
        model_path = config.pretrained_path
        self.swin_transformer = SwinTransformer(img_size,in_chans = 3)
        checkpoint = torch.load(model_path, map_location=torch.device('cpu'))['model']
        unexpected = ["patch_embed.proj.weight", "patch_embed.proj.bias", "patch_embed.norm.weight", "patch_embed.norm.bias",
                     "head.weight", "head.bias", "layers.0.downsample.norm.weight", "layers.0.downsample.norm.bias",
                     "layers.0.downsample.reduction.weight", "layers.1.downsample.norm.weight", "layers.1.downsample.norm.bias",
                     "layers.1.downsample.reduction.weight", "layers.2.downsample.norm.weight", "layers.2.downsample.norm.bias",
                     "layers.2.downsample.reduction.weight"]
        for key in list(checkpoint.keys()):
            if key in unexpected:
                del checkpoint[key]
        self.swin_transformer.load_state_dict(checkpoint)
        

        resnet = eval(f"torchvision.models.{config.cnn_backbone}(pretrained={config.resnet_pretrained_path})")
        self.resnet_layers = nn.ModuleList(resnet.children())[:8]
        
        self.p1_ch = nn.Conv2d(config.cnn_pyramid_fm[0], config.swin_pyramid_fm[0] , kernel_size = 1)
        self.p1_pm = PatchMerging((config.image_size // config.patch_size, config.image_size // config.patch_size), config.swin_pyramid_fm[0])
        
        self.p2 = self.resnet_layers[5]
        self.p2_ch = nn.Conv2d(config.cnn_pyramid_fm[1], config.swin_pyramid_fm[1] , kernel_size = 1)
        self.p2_pm = PatchMerging((config.image_size // config.patch_size // 2, config.image_size // config.patch_size // 2), config.swin_pyramid_fm[1])
        
        
        self.proj1_2 = nn.Linear(config.swin_pyramid_fm[0], config.swin_pyramid_fm[1])
        self.proj3_4 = nn.Linear(config.swin_pyramid_fm[3], config.swin_pyramid_fm[2])
        
        
        self.p3 = self.resnet_layers[6]
        self.p3_ch = nn.Conv2d(config.cnn_pyramid_fm[2] , config.swin_pyramid_fm[2] , kernel_size =  1)
        self.p3_pm = PatchMerging((config.image_size // config.patch_size // 4,config.image_size // config.patch_size // 4), config.swin_pyramid_fm[2])
        
        
        self.p4 = self.resnet_layers[7]
        self.p4_ch = nn.Conv2d(config.cnn_pyramid_fm[3] , config.swin_pyramid_fm[3] , kernel_size = 1)
        

    def forward(self, x):
        
        for i in range(5):
            x = self.resnet_layers[i](x) 
        
        # 1
        fm1 = x
        fm1_ch = self.p1_ch(x)
        fm1_reshaped = Rearrange('b c h w -> b (h w) c')(fm1_ch)               
        sw1 = self.swin_transformer.layers[0](fm1_reshaped)
        sw1_skipped = fm1_reshaped  + sw1
        fm1_sw1 = self.p1_pm(sw1_skipped)
        
        #2
        fm1_sw2 = self.swin_transformer.layers[1](fm1_sw1)
        fm2 = self.p2(fm1)
        fm2_ch = self.p2_ch(fm2)
        fm2_reshaped = Rearrange('b c h w -> b (h w) c')(fm2_ch) 
        fm2_sw2_skipped = fm2_reshaped  + fm1_sw2
        fm2_sw2 = self.p2_pm(fm2_sw2_skipped)
    
        # Concat 1,2
        sw1_skipped_projected = self.proj1_2(sw1_skipped)
        concat1 = torch.cat((sw1_skipped_projected, fm2_sw2_skipped), dim = 1)
        
        #3
        fm2_sw3 = self.swin_transformer.layers[2](fm2_sw2)
        fm3 = self.p3(fm2)
        fm3_ch = self.p3_ch(fm3)
        fm3_reshaped = Rearrange('b c h w -> b (h w) c')(fm3_ch) 
        fm3_sw3_skipped = fm3_reshaped  + fm2_sw3
        fm3_sw3 = self.p3_pm(fm3_sw3_skipped)
        
        #4
        fm3_sw4 = self.swin_transformer.layers[3](fm3_sw3)
        fm4 = self.p4(fm3)
        fm4_ch = self.p4_ch(fm4)
        fm4_reshaped = Rearrange('b c h w -> b (h w) c')(fm4_ch) 
        fm4_sw4_skipped = fm4_reshaped  + fm3_sw4
        
        #concat 3,4
        sw4_skipped_projected = self.proj3_4(fm4_sw4_skipped)
        concat2 = torch.cat((sw4_skipped_projected, fm3_sw3_skipped), dim = 1)
        
        return [concat1, concat2], [sw1_skipped, fm2_sw2_skipped, fm3_sw3_skipped, fm4_sw4_skipped]


class All2Cross(nn.Module):
    def __init__(self, config, img_size = 224 , in_chans=3, embed_dim=(192, 384),
                 depth=([1, 4, 0], [1, 4, 0]), num_heads=(6, 6), mlp_ratio=(4., 4., 1.),
                 qkv_bias=False, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., norm_layer=nn.LayerNorm):
        super().__init__()
        self.pyramid = PyramidFeatures(config=config, img_size= img_size, in_channels=in_chans)
        
        self.attention1 = Attention(embed_dim[0])
        self.attention2 = Attention(embed_dim[1])
        
        self.cls_token_1_2 = nn.Parameter(torch.zeros(1,1,embed_dim[0]))
        self.cls_token_3_4 = nn.Parameter(torch.zeros(1,1,embed_dim[1]))
        self.cls_token = nn.ParameterList([self.cls_token_1_2, self.cls_token_3_4])
        
        n_p1 = (config.image_size // config.patch_size) ** 2 + (config.image_size // config.patch_size // 2) ** 2 # default: 3920 
        n_p2 = (config.image_size // config.patch_size // 4) ** 2 + (config.image_size // config.patch_size // 8) ** 2  # default: 245 
        num_patches = (n_p1, n_p2)
        self.num_branches = 2
        
        
        total_depth = sum([sum(x[-2:]) for x in depth])
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, total_depth)]  # stochastic depth decay rule
        dpr_ptr = 0
        self.blocks = nn.ModuleList()
        for idx, block_config in enumerate(depth):
            curr_depth = max(block_config[:-1]) + block_config[-1]
            dpr_ = dpr[dpr_ptr:dpr_ptr + curr_depth]
            blk = MultiScaleBlock(embed_dim, num_patches, block_config, num_heads=num_heads, mlp_ratio=mlp_ratio,
                                  qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr_,
                                  norm_layer=norm_layer)
            dpr_ptr += curr_depth
            self.blocks.append(blk)

        self.norm = nn.ModuleList([norm_layer(embed_dim[i]) for i in range(self.num_branches)])

        for i in range(self.num_branches):
            trunc_normal_(self.cls_token[i], std=.02)
        self.apply(self._init_weights)
    
    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
    
    def forward(self, x):
        B, C, H, W = x.shape
        cls_token_1_2 = self.cls_token[0].expand(B, -1, -1)
        cls_token_3_4 = self.cls_token[1].expand(B, -1, -1) 
        concats, skips = self.pyramid(x)
        concat1, concat2 = concats

        concat1 = torch.cat((cls_token_1_2, concat1), dim = 1)
        concat2 = torch.cat((cls_token_3_4, concat2), dim = 1)
        
        attn1 = self.attention1(concat1)
        attn2 = self.attention2(concat2)
        
        xs = [attn1, attn2]
        
        for blk in self.blocks:
            xs = blk(xs)
        xs = [self.norm[i](x) for i, x in enumerate(xs)]
        out = [x[:, 0] for x in xs]
        return xs


class ConvUpsample(nn.Module):
    def __init__(self, in_chans=384, out_chans=[128], upsample=True):
        super().__init__()
        self.in_chans = in_chans
        self.out_chans = out_chans
        
        self.conv_tower = nn.ModuleList()
        for i, out_ch in enumerate(self.out_chans):
            if i>0: self.in_chans = out_ch
            self.conv_tower.append(nn.Conv2d(
                self.in_chans, out_ch,
                kernel_size=3, stride=1,
                padding=1, bias=False
            ))
            self.conv_tower.append(nn.GroupNorm(32, out_ch))
            self.conv_tower.append(nn.ReLU(inplace=False))
            if upsample:
                self.conv_tower.append(nn.Upsample(
                        scale_factor=2, mode='bilinear', align_corners=False))
            
        self.convs_level = nn.Sequential(*self.conv_tower)
        
    def forward(self, x):
        return self.convs_level(x)


class SegmentationHead(nn.Sequential):
    def __init__(self, in_channels, out_channels, kernel_size=3):
        conv2d = nn.Conv2d(in_channels, out_channels, kernel_size=kernel_size, padding=kernel_size // 2)
        super().__init__(conv2d)


class SwinRetina_V1(nn.Module):
    def __init__(self, config, img_size=224, in_chans=3, n_classes=16):
        super().__init__()
        self.img_size = img_size
        self.patch_size = [[4,8],[16,32]]

        self.n_classes = n_classes
        self.All2Cross = All2Cross(config = config, img_size= img_size, in_chans=in_chans)
        
        self.conv_list = nn.ModuleList([nn.ModuleList([ConvUpsample(in_chans=192, out_chans=[128], upsample=False),
                                        ConvUpsample(in_chans=192, out_chans=[128])]),
                                        nn.ModuleList([ConvUpsample(in_chans=384, out_chans=[128,128]),
                                        ConvUpsample(in_chans=384, out_chans=[128,128,128])])])

        self.segmentation_head = SegmentationHead(
            in_channels=16,
            out_channels=n_classes,
            kernel_size=3,
        )    

        self.conv_pred = nn.Sequential(
            nn.Conv2d(
                128, 16,
                kernel_size=1, stride=1,
                padding=0, bias=True),
#             nn.GroupNorm(8, self.n_classes), 
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=4, mode='bilinear', align_corners=False)
        )
        
    def forward(self, x):
        xs = self.All2Cross(x)
        embeddings = [x[:, 1:] for x in xs]

        reshaped_embed = []
        for i, embed in enumerate(embeddings):
            embed_l = embed[:, : (self.img_size//self.patch_size[i][0])**2,:]
            embed_s = embed[:, (self.img_size//self.patch_size[i][0])**2:,:]

            value_l = Rearrange('b (h w) d -> b d h w', h=(self.img_size//self.patch_size[i][0]), w=(self.img_size//self.patch_size[i][0]))(embed_l)
            value_s = Rearrange('b (h w) d -> b d h w', h=(self.img_size//self.patch_size[i][1]), w=(self.img_size//self.patch_size[i][1]))(embed_s)
            
            conv_value_l = self.conv_list[i][0](value_l)
            conv_value_s = self.conv_list[i][1](value_s)
                        
            value_sum = conv_value_l + conv_value_s
            reshaped_embed.append(value_sum)
            
        C = reshaped_embed[0] + reshaped_embed[1]
        C = self.conv_pred(C)
        
        out = self.segmentation_head(C)
        
        return out

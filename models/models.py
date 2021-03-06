import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import torch.nn.init as init
from torch.autograd import Variable
from torch.nn.parameter import Parameter
#from minibatch_discrim import MiniBatchDiscrimination

# ResNet module to process the incoming filters. We are using Instance Norm replacing traditional BatchNorm.
# BatchNorm doesn't plays any significant role, since our batch is very small, another thing we observed is 
# that the feature maps don't face covariate shift in ResNet block as the dataset are very close to each other.
# removing Norm from ResNet block doesn't affects the model resutl.

class ResidualBlock(nn.Module):
    def __init__(self, in_features, out_features):
        super(ResidualBlock, self).__init__()

        conv_block = [nn.ReflectionPad2d(1),
                      nn.Conv2d(in_features, in_features, 3),
                      nn.InstanceNorm2d(in_features),
                      nn.ReLU(inplace=True),
                      nn.ReflectionPad2d(1),
                      nn.Conv2d(in_features, out_features, 3),
                      nn.InstanceNorm2d(out_features)]

        self.conv_block = nn.Sequential(*conv_block)

    def forward(self, x):
        return x + self.conv_block(x)

    

class ConvBlock(nn.Module):
    def __init__(self, in_features, out_features):
        super(ConvBlock, self).__init__()

        conv_block = [nn.ReflectionPad2d(1),
                      nn.Conv2d(in_features, in_features, 3),
                      nn.InstanceNorm2d(in_features),
                      nn.ReLU(inplace=True),
                      nn.ReflectionPad2d(1),
                      nn.Conv2d(in_features, out_features, 3),
                      nn.InstanceNorm2d(out_features)]

        self.conv_block = nn.Sequential(*conv_block)

    def forward(self, x):
        return self.conv_block(x)

# Over the cause of GAN history we did infer that if we replace unknown region with Noise, then GANs can effectively 
# generate the missing regions (effectively implies to generate something).
# We didn't test with different Noise, and their affects in detail.

class NoiseInjection(nn.Module):
    def __init__(self, channel):
        super().__init__()

        self.weight = nn.Parameter(torch.zeros(1, channel, 1, 1))

    def forward(self, image, mask):
        #         pdb.set_trace()
        noise = torch.randn(1, 1, image.shape[2], image.shape[3]).cuda()
        mask = mask[:, :1, :, :].repeat(1, image.shape[1], 1, 1)
        return image + self.weight * noise * mask


    
# model_ds downsamples the feature maps, we use stride = 2 to downsample feature maps instead of 
# max pooling layer which is not learnable.

class model_ds(nn.Module):
    def __init__(self, in_features, out_features):
        super(model_ds, self).__init__()

        conv_block = [nn.Conv2d(in_features, out_features, 3, stride=2, padding=1),
                      nn.InstanceNorm2d(out_features),
                      nn.ReLU(inplace=True)]

        self.conv_block = nn.Sequential(*conv_block)

    def forward(self, x):
        return self.conv_block(x)


 # model_up Upsamples the feature maps again with a layer which is learnable, we didn't use any other method since 
# nn.Upsample has no learnable weights, the other layer that we could have tried is sub-pixel which also learns to 
# upsample / downsmaple. 

    
class model_up(nn.Module):
    def __init__(self, in_features, out_features):
        super(model_up, self).__init__()

        conv_block = [nn.ConvTranspose2d(in_features, out_features, 3, stride=2, padding=1, output_padding=1),
                      nn.InstanceNorm2d(out_features),
                      nn.ReLU(inplace=True)]

        self.conv_block = nn.Sequential(*conv_block)

    def forward(self, x):
        return self.conv_block(x)


def swish(x):
    return x * F.sigmoid(x)


def get_mean_var(c):
    n_batch, n_ch, h, w = c.size()

    c_view = c.view(n_batch, n_ch, h * w)
    c_mean = c_view.mean(2)

    c_mean = c_mean.view(n_batch, n_ch, 1, 1).expand_as(c)
    c_var = c_view.var(2)
    c_var = c_var.view(n_batch, n_ch, 1, 1).expand_as(c)
    # c_var = c_var * (h * w - 1) / float(h * w)  # unbiased variance

    return c_mean, c_var


class transform_layer(nn.Module):

    def __init__(self, input_nc, in_features, out_features):
        super(transform_layer, self).__init__()
        self.channels = in_features

        self.convblock = ConvBlock(in_features + in_features, out_features)
        self.up_conv = nn.Conv2d(in_features * 2, in_features, 3, 1, 1)
        self.down_conv = nn.Sequential(
            nn.Conv2d(64, in_features // 4, 3, 1, 1),
            nn.ReLU(),
            nn.Conv2d(in_features // 4, in_features // 2, 1, 1),
            nn.ReLU(),
            nn.Conv2d(in_features // 2, in_features, 1, 1),
            nn.ReLU()
        )
        self.noise = NoiseInjection(in_features)

        self.convblock_ = ConvBlock(in_features + 64, out_features)

        self.vgg_block = nn.Sequential(
            nn.Conv2d(input_nc, 16, 3, 1, 1),
            nn.ReLU(),
            nn.Conv2d(16, 32, 1, 1),
            nn.ReLU(),
            nn.Conv2d(32, 64, 1, 1),
            nn.ReLU()
        )

    def forward(self, x, mask=None, style=None, mode='D'):
        #         pdb.set_trace()
        if mode == 'C':
            style = F.upsample(style, size=(x.shape[2], x.shape[2]), mode='bilinear')

            style = self.vgg_block(style)
            concat = torch.cat([x, style], 1)

            out = (self.convblock_(concat))
            return out, style
        else:
            mask = F.upsample(mask, size=(x.shape[2], x.shape[2]), mode='bilinear')
            x = self.noise(x, mask)
            #             style = F.upsample(style, size=(x.shape[2],x.shape[2]), mode='bilinear')

            style = self.down_conv(style)
            concat = torch.cat([x, style], 1)

            out = (self.convblock(concat) + style)
            return out


class transform_up_layer(nn.Module):

    def __init__(self, in_features, out_features, diff=False):
        super(transform_up_layer, self).__init__()
        self.channels = in_features

        if diff == True:
            self.convblock = ConvBlock(in_features * 2 + in_features, out_features)
        else:
            self.convblock = ConvBlock(in_features * 2, out_features)
        self.up_conv = nn.Sequential(
            nn.Conv2d(in_features * 2, in_features, 3, 1, 1),
            nn.ReLU()
        )

    def forward(self, x, y, mode="down"):

        #print("x shape-", x.shape)
        #print("y shape-", y.shape)
        y = self.up_conv(y)
        #print("new y shape-", y.shape)
        concat = torch.cat([x, y], 1)

        out = self.convblock(concat)

        #         out = self.adain(out,style)

        return out

class transform_up_layer_extended(nn.Module):

    def __init__(self, in_features, out_features, diff=False):
        super(transform_up_layer_extended, self).__init__()
        self.channels = in_features

        if diff == True:
            self.convblock = ConvBlock(in_features * 2 + in_features, out_features)
        else:
            self.convblock = ConvBlock(in_features * 2, out_features)
        
        self.up_conv = nn.Sequential(
            nn.Conv2d(in_features * 2, in_features, 3, 1, 1),
            nn.ReLU()
        )
        self.up_conv1 = nn.Sequential(
            nn.Conv2d(in_features, (in_features//2), 3, 1, 1),
            nn.ReLU()
        )
        self.up_conv2 = nn.Sequential(
            nn.Conv2d(in_features // 2, in_features // 4, 3, 1, 1),
            nn.ReLU()
        )
        self.up_deconv1 = nn.Sequential(
            nn.Conv2d((in_features), in_features * 2, 3, 1, 1),
            nn.ReLU()
        )
        self.up_deconv2 = nn.Sequential(
            nn.Conv2d(in_features * 2, in_features * 4, 3, 1, 1),
            nn.ReLU()
        )
        self.up_deconv3 = nn.Sequential(
            nn.Conv2d((in_features//2), in_features, 3, 1, 1),
            nn.ReLU()
        )

    def forward(self, x, y, mode="down"):

        y = F.pad(y, (0,(x.shape[2] - y.shape[2]),0,(x.shape[3] - y.shape[3])), "constant", 0)
        #print("x shape-", x.shape)
        #print("y shape-", y.shape)

        count = int(y.shape[1] // x.shape[1])
        count = count // 2
        #print("count-", count)


        for i in range(count):
            if i == 0:
                #print("before1-", y.shape)
                y = self.up_conv(y)
                #print("after1-", y.shape)
            elif i == 1:
                #print("before2-", y.shape)
                y = self.up_conv1(y)
                #print("after2-", y.shape)
            elif i == 2:
                #print("before2-", y.shape)
                y = self.up_conv2(y)
                #print("after2-", y.shape)


        #print("new y shape-", y.shape)
        concat = torch.cat([x, y], 1)

        #print("concat shape-", concat.shape)

        for i in range(count):
            if i == 0:
                pass
            elif i == 1 and count == 2:
                #print("deconv before1-", concat.shape)
                concat = self.up_deconv1(concat)
                #print("deconv after1-", concat.shape)
            elif i == 1 and count == 4:
                #print("deconv3 before2-", concat.shape)
                concat = self.up_deconv3(concat)
                #print("deconv3 after2-", concat.shape)
            elif i == 2:
                #print("deconv before3-", concat.shape)
                concat = self.up_deconv1(concat)
                #print("deconv after3-", concat.shape)

        out = self.convblock(concat)

        #         out = self.adain(out,style)
        #out = self.up_conv(out)
        for i in range(count):
            if i == 0:
                #print("out before1-", out.shape)
                out = self.up_conv(out)
                #print("out after1-", out.shape)
            elif i == 1:
                #print(" out before2-", out.shape)
                out = self.up_conv1(out)
                #print("out after2-", out.shape)
            elif i == 2:
                #print("out before2-", out.shape)
                out = self.up_conv2(out)
                #print("out after2-", out.shape)

        return out


class GeneratorCoarse(nn.Module):
    def __init__(self, input_nc, output_nc, n_residual_blocks=1):
        super(GeneratorCoarse, self).__init__()
        in_features = 64

        self.model_input_cloth = nn.Sequential(
            nn.ReflectionPad2d(3),
            nn.Conv2d(input_nc + 1, in_features, 7),
            nn.InstanceNorm2d(in_features),
            nn.ReLU(inplace=True)
        )

        self.block128 = nn.Sequential(
            ResidualBlock(in_features, in_features)
        )
        self.block128_transform = transform_layer(input_nc, in_features, in_features)

        self.block64 = nn.Sequential(
            model_ds(in_features, in_features * 2),
            ResidualBlock(in_features * 2, in_features * 2)
        )
        self.block64_transform = transform_layer(input_nc, in_features * 2, in_features * 2)

        self.block32 = nn.Sequential(
            model_ds(in_features * 2, in_features * 4),
            ResidualBlock(in_features * 4, in_features * 4)
        )
        self.block32_transform = transform_layer(input_nc, in_features * 4, in_features * 4)

        self.block16 = nn.Sequential(
            model_ds(in_features * 4, in_features * 8),
            ResidualBlock(in_features * 8, in_features * 8)
        )
        self.block16_transform = transform_layer(input_nc, in_features * 8, in_features * 8)
        self.block8 = nn.Sequential(
            model_ds(in_features * 8, in_features * 8),
            ResidualBlock(in_features * 8, in_features * 8)
        )
        self.block8_transform = transform_layer(input_nc, in_features * 8, in_features * 8)
        self.block4 = nn.Sequential(
            model_ds(in_features * 8, in_features * 8),
            ResidualBlock(in_features * 8, in_features * 8)
        )
        self.block4_transform = transform_layer(input_nc, in_features * 8, in_features * 8)

        self.block4_up = nn.Sequential(
            nn.Conv2d(in_features * 8, in_features * 4, 3, 1, 1),
            ResidualBlock(in_features * 4, in_features * 4)
        )
        self.block4_up_transform = transform_up_layer(in_features * 4, in_features * 8)

        self.block8_up = nn.Sequential(
            model_up(in_features * 8, in_features * 4),
            ResidualBlock(in_features * 4, in_features * 4)
        )
        self.block8_up_transform = transform_up_layer(in_features * 4, in_features * 8)

        self.block16_up = nn.Sequential(
            model_up(in_features * 8, in_features * 4),
            ResidualBlock(in_features * 4, in_features * 4)
        )
        self.block16_up_transform = transform_up_layer(in_features * 4, in_features * 8)

        self.block32_new_up_transform = transform_up_layer_extended(in_features * 4, in_features * 8)
        self.block64_new_up_transform = transform_up_layer_extended(in_features * 4, in_features * 8)
        self.block128_new_up_transform = transform_up_layer_extended(in_features * 4, in_features * 8)

        self.block32_up = nn.Sequential(
            model_up(in_features * 8, in_features * 4),
            ResidualBlock(in_features * 4, in_features * 4)
        )
        self.block32_up_transform = transform_up_layer(in_features * 2, in_features * 4, True)

        self.block64_up = nn.Sequential(
            model_up(in_features * 4, in_features * 2),
            ResidualBlock(in_features * 2, in_features * 2)
        )
        self.block64_up_transform = transform_up_layer(in_features, in_features * 2, True)

        self.block128_up = nn.Sequential(
            model_up(in_features * 2, in_features),
            ResidualBlock(in_features, in_features)
        )
        self.block128_up_transform = transform_up_layer(in_features // 2, in_features, True)

        self.model_output = nn.Sequential(
            nn.ReflectionPad2d(3),
            nn.Conv2d(in_features, output_nc, 7),
            nn.Tanh()
        )

    def forward(self, src, *input):
        conds = []
        for cond in input:
            conds.append(cond)
        conds.append(src)
        style = torch.cat(conds, 1)

        #uncomment for stage 2 training
        style = torch.narrow(style, 1, 3, 6)

        y = torch.cat([torch.randn(1, 1, src.shape[2], src.shape[3]).cuda(), style], 1)

        y = self.model_input_cloth(y)

        y128 = self.block128(y)
        y128, s_128 = self.block128_transform(x=y128, style=style, mode="C")

        y64 = self.block64(y128)
        y64, s_64 = self.block64_transform(x=y64, style=style, mode="C")

        y32 = self.block32(y64)
        y32, s_32 = self.block32_transform(x=y32, style=style, mode="C")

        y16 = self.block16(y32)
        y16, s_16 = self.block16_transform(x=y16, style=style, mode="C")

        y8 = self.block8(y16)
        y8, s_8 = self.block8_transform(x=y8, style=style, mode="C")

        y4 = self.block4(y8)
        y4, s_4 = self.block4_transform(x=y4, style=style, mode="C")

        ############## Decoder #######################

        y4u = self.block4_up(y4)
        y4u = self.block4_up_transform(y4u, y4)

        y8u = self.block8_up(y4u)
        y8u = self.block8_up_transform(y8u, y8)

        y16u = self.block16_up(y8u)
        y16u = self.block16_up_transform(y16u, y16)

        y32u = self.block32_up(y16u)
        #print("layer 32 og shape-", y32u.shape)
        y32u = self.block32_new_up_transform(y32u, y16)
        #print("layer 32 new shape-", y32u.shape)

        y64u = self.block64_up(y32u)
        #print("layer 64 og shape-", y64u.shape)
        y64u = self.block64_new_up_transform(y64u, y16)
        #print("layer 64 new shape-", y64u.shape)

        y128u = self.block128_up(y64u)
        #print("layer 128 og shape-", y128u.shape)
        y128u = self.block128_new_up_transform(y128u, y16)
        #print("layer 128 new shape-", y128u.shape)

        out = self.model_output(y128u)

        return out, s_128, s_64, s_32, s_16, s_8, s_4


class GeneratorStitch(nn.Module):
    def __init__(self, input_nc, output_nc, n_residual_blocks=1):
        super(GeneratorStitch, self).__init__()
        in_features = 64
        self.model_input_full = nn.Sequential(
            nn.ReflectionPad2d(3),
            nn.Conv2d(input_nc, in_features, 7),
            nn.InstanceNorm2d(in_features),
            nn.ReLU(inplace=True)
        )
        self.model_input_cloth = nn.Sequential(
            nn.ReflectionPad2d(3),
            nn.Conv2d(input_nc + 1, in_features, 7),
            nn.InstanceNorm2d(in_features),
            nn.ReLU(inplace=True)
        )

        self.block128 = nn.Sequential(
            ResidualBlock(in_features, in_features)
        )
        self.block128_transform = transform_layer(input_nc, in_features, in_features)

        self.block64 = nn.Sequential(
            model_ds(in_features, in_features * 2),
            ResidualBlock(in_features * 2, in_features * 2)
        )
        self.block64_transform = transform_layer(input_nc, in_features * 2, in_features * 2)

        self.block32 = nn.Sequential(
            model_ds(in_features * 2, in_features * 4),
            ResidualBlock(in_features * 4, in_features * 4)
        )
        self.block32_transform = transform_layer(input_nc, in_features * 4, in_features * 4)

        self.block16 = nn.Sequential(
            model_ds(in_features * 4, in_features * 8),
            ResidualBlock(in_features * 8, in_features * 8)
        )
        self.block16_transform = transform_layer(input_nc, in_features * 8, in_features * 8)
        self.block8 = nn.Sequential(
            model_ds(in_features * 8, in_features * 8),
            ResidualBlock(in_features * 8, in_features * 8)
        )
        self.block8_transform = transform_layer(input_nc, in_features * 8, in_features * 8)
        self.block4 = nn.Sequential(
            model_ds(in_features * 8, in_features * 8),
            ResidualBlock(in_features * 8, in_features * 8)
        )
        self.block4_transform = transform_layer(input_nc, in_features * 8, in_features * 8)

        self.block4_up = nn.Sequential(
            nn.Conv2d(in_features * 8, in_features * 4, 3, 1, 1),
            ResidualBlock(in_features * 4, in_features * 4)
        )
        self.block4_up_transform = transform_up_layer(in_features * 4, in_features * 8)

        self.block8_up = nn.Sequential(
            model_up(in_features * 8, in_features * 4),
            ResidualBlock(in_features * 4, in_features * 4)
        )
        self.block8_up_transform = transform_up_layer(in_features * 4, in_features * 8)

        self.block16_up = nn.Sequential(
            model_up(in_features * 8, in_features * 4),
            ResidualBlock(in_features * 4, in_features * 4)
        )
        self.block16_up_transform = transform_up_layer(in_features * 4, in_features * 8)

        self.block32_up = nn.Sequential(
            model_up(in_features * 8, in_features * 4),
            ResidualBlock(in_features * 4, in_features * 4)
        )
        self.block32_up_transform = transform_up_layer(in_features * 2, in_features * 4, True)

        self.block64_up = nn.Sequential(
            model_up(in_features * 4, in_features * 2),
            ResidualBlock(in_features * 2, in_features * 2)
        )
        self.block64_up_transform = transform_up_layer(in_features, in_features * 2, True)

        self.block128_up = nn.Sequential(
            model_up(in_features * 2, in_features),
            ResidualBlock(in_features, in_features)
        )
        self.block128_up_transform = transform_up_layer(in_features // 2, in_features, True)

        self.model_output = nn.Sequential(
            nn.ReflectionPad2d(3),
            nn.Conv2d(in_features, output_nc, 7),
            nn.Tanh()
        )

    def forward(self, src, *input):
        conds = []
        for cond in input:
            conds.append(cond)
        conds.append(src)
        style = torch.cat(conds, 1)
        y = torch.cat([torch.randn(1, 1, src.shape[2], src.shape[3]).cuda(), style], 1)
        
 ############## Encoder #######################
        y = self.model_input_cloth(y)

        y128 = self.block128(y)
        y128, s_128 = self.block128_transform(x=y128, style=style, mode="C")

        y64 = self.block64(y128)
        y64, s_64 = self.block64_transform(x=y64, style=style, mode="C")

        y32 = self.block32(y64)
        y32, s_32 = self.block32_transform(x=y32, style=style, mode="C")

        y16 = self.block16(y32)
        y16, s_16 = self.block16_transform(x=y16, style=style, mode="C")

        y8 = self.block8(y16)
        y8, s_8 = self.block8_transform(x=y8, style=style, mode="C")

        y4 = self.block4(y8)
        y4, s_4 = self.block4_transform(x=y4, style=style, mode="C")

        ############## Decoder #######################

        y4u = self.block4_up(y4)
        y4u = self.block4_up_transform(y4u, y4)

        y8u = self.block8_up(y4u)
        y8u = self.block8_up_transform(y8u, y8)

        y16u = self.block16_up(y8u)
        y16u = self.block16_up_transform(y16u, y16)

        y32u = self.block32_up(y16u)
        y32u = self.block32_up_transform(y32u, y32)

        y64u = self.block64_up(y32u)
        y64u = self.block64_up_transform(y64u, y64)

        y128u = self.block128_up(y64u)
        y128u = self.block128_up_transform(y128u, y128)

        out = self.model_output(y128u)

        return out, s_128, s_64, s_32, s_16, s_8, s_4

    
    
class MiniBatchDiscrimination(nn.Module):
    def __init__(self, in_features, out_features, kernel_dims):
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.kernel_dims = kernel_dims

        self.T = nn.Parameter(torch.Tensor(in_features, out_features, kernel_dims))
        init.normal_(self.T, 0, 1)

    def forward(self, x):
        # x is NxA
        # T is AxBxC
        matrices = x.mm(self.T.view(self.in_features, -1))
        matrices = matrices.view(-1, self.out_features, self.kernel_dims)

        M = matrices.unsqueeze(0)  # 1xNxBxC
        M_T = M.permute(1, 0, 2, 3)  # Nx1xBxC
        norm = torch.abs(M - M_T).sum(3)  # NxNxB
        expnorm = torch.exp(-norm)
        o_b = (expnorm.sum(0) - 1)   # NxB, subtract self distance

        x = torch.cat([x, o_b], 1)
        return x



# Discriminator
# https://github.com/aitorzip/PyTorch-SRGAN/blob/master/models.py
class Discriminator(nn.Module):
    def __init__(self):
        super(Discriminator, self).__init__()
        self.conv1 = nn.Conv2d(3, 64, 3, stride=1, padding=1)

        self.conv2 = nn.Conv2d(64, 64, 3, stride=2, padding=1)
        self.bn2 = nn.InstanceNorm2d(64)
        self.conv3 = nn.Conv2d(64, 128, 3, stride=1, padding=1)
        self.bn3 = nn.InstanceNorm2d(128)
        self.conv4 = nn.Conv2d(128, 128, 3, stride=2, padding=1)
        self.bn4 = nn.InstanceNorm2d(128)
        self.conv5 = nn.Conv2d(128, 256, 3, stride=1, padding=1)
        self.bn5 = nn.InstanceNorm2d(256)
        self.conv6 = nn.Conv2d(256, 256, 3, stride=2, padding=1)
        self.bn6 = nn.InstanceNorm2d(256)
        self.conv7 = nn.Conv2d(256, 512, 3, stride=1, padding=1)
        self.bn7 = nn.InstanceNorm2d(512)
        self.conv8 = nn.Conv2d(512, 512, 3, stride=2, padding=1)
        self.bn8 = nn.InstanceNorm2d(512)

        # Replaced original paper FC layers with FCN
        self.conv9 = nn.Conv2d(512, 1, 1, stride=1, padding=1)

        self.mbd = MiniBatchDiscrimination(10, 10, 50) #BATCH_SIZE)

    def forward(self, x):
        #print("og-", x.shape)
        x = swish(self.conv1(x))

        x = swish(self.bn2(self.conv2(x)))
        x = swish(self.bn3(self.conv3(x)))
        x = swish(self.bn4(self.conv4(x)))
        x = swish(self.bn5(self.conv5(x)))
        x = swish(self.bn6(self.conv6(x)))
        x = swish(self.bn7(self.conv7(x)))
        x = swish(self.bn8(self.conv8(x)))

        temp = self.conv9(x)
        temp = torch.squeeze(temp)
        temp = self.mbd(temp)
        temp = torch.unsqueeze(temp, dim=0)
        temp = torch.unsqueeze(temp, dim=0)
        #print("discrim shape-", temp.shape)

        x = self.conv9(x)
        #print("x shape needed-", x.shape)
        x = F.pad(x, (0,10,0,0), "constant", 0)
        #print("x shape-", x.shape)

        x = torch.cat((x, temp),dim=1) 
        #print("cat shape-", x.shape)
        x = torch.mean(x, dim=1)
        x = torch.unsqueeze(x, dim=0)
        #print("new x shape-", x.shape)
        
        x = F.sigmoid(F.avg_pool2d(x, x.size()[2:])).view(x.size()[0], -1)
        #print("final shape", x.shape)
      
        return x

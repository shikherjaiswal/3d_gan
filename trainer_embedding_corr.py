import glob
import os
import time
import itertools

import torch
import torch.nn as nn
import torch.utils
import torchvision
import torch.nn.functional as F
from tensorboard_logger import configure, log_value
from torch.autograd import Variable
from torch.utils.data import DataLoader

from dataset import LRWdataset, VaganDataset
from model_corr import Discriminator, Generator
from corr_encoder import FlowEncoder, AudioDeriEncoder


class Trainer():
    def __init__(self, config):
        self.generator = Generator()
        self.discriminator = Discriminator()
        self.images_encoder = FlowEncoder('embedding')
        self.audio_encoder = AudioDeriEncoder(need_deri=False)

        print(self.generator)
        print(self.discriminator)
        self.bce_loss_fn = nn.BCELoss()
        self.mse_loss_fn = nn.MSELoss()
        # self.cosine_loss_fn = CosineSimiLoss(config.batch_size)
        self.cosine_loss_fn = nn.CosineEmbeddingLoss()
        
        self.opt_g = torch.optim.Adam([p for p in self.generator.parameters() if p.requires_grad],
                                      lr=config.lr, betas=(config.beta1, config.beta2))
        self.opt_d = torch.optim.Adam(self.discriminator.parameters(),
                                      lr=config.lr, betas=(config.beta1, config.beta2))
        self.opt_corr = torch.optim.Adam(itertools.chain(self.images_encoder.parameters(),
                                                         self.audio_encoder.parameters()),
                                         lr=config.lr_corr, betas=(0.9, 0.999), weight_decay=0.0005)

        if config.dataset == 'grid':
            self.dataset = VaganDataset(
                config.dataset_dir, train=config.is_train)
        elif config.dataset == 'lrw':
            self.dataset = LRWdataset(
                config.dataset_dir, train=config.is_train)

        self.data_loader = DataLoader(self.dataset,
                                      batch_size=config.batch_size,
                                      num_workers=config.num_thread,
                                      shuffle=True, drop_last=True)

        self.ones = Variable(torch.ones(config.batch_size))
        self.zeros = Variable(torch.zeros(config.batch_size))
        self.one_corr = Variable(torch.ones(config.batch_size))

        if config.cuda:
            device_ids = [int(i) for i in config.device_ids.split(',')]
            if len(device_ids) == 1:
                self.generator = self.generator.cuda(device_ids[0])
                self.discriminator = self.discriminator.cuda(device_ids[0])
                self.images_encoder = self.images_encoder.cuda(device_ids[0])
                self.audio_encoder = self.audio_encoder.cuda(device_ids[0])
            else:
                self.generator = nn.DataParallel(
                    self.generator.cuda(), device_ids=device_ids)
                self.discriminator = nn.DataParallel(
                    self.discriminator.cuda(), device_ids=device_ids)
                self.images_encoder = nn.DataParallel(self.images_encoder.cuda(), device_ids=device_ids)
                self.audio_encoder = nn.DataParallel(self.audio_encoder.cuda(), device_ids=device_ids)
            self.bce_loss_fn = self.bce_loss_fn.cuda()
            self.mse_loss_fn = self.mse_loss_fn.cuda()
            self.cosine_loss_fn = self.cosine_loss_fn.cuda()
            self.ones = self.ones.cuda(async=True)
            self.zeros = self.zeros.cuda(async=True)
            self.one_corr = self.one_corr.cuda(async=True)

        self.config = config
        self.start_epoch = 0

    def fit(self):
        config = self.config
        configure("{}".format(config.log_dir), flush_secs=5)

        num_steps_per_epoch = len(self.data_loader)
        cc = 0

        for epoch in range(self.start_epoch, config.max_epochs):
            for step, (example, real_im, landmarks, right_audio, wrong_audio) in enumerate(self.data_loader):
                t1 = time.time()

                if config.cuda:
                    example = Variable(example).cuda(async=True)
                    real_im = Variable(real_im).cuda(async=True)
                    right_audio = Variable(right_audio).cuda(async=True)
                    wrong_audio = Variable(wrong_audio).cuda(async=True)
                else:
                    example = Variable(example)
                    real_im = Variable(real_im)
                    right_audio = Variable(right_audio)
                    wrong_audio = Variable(wrong_audio)

                fake_im, _ = self.generator(example, right_audio)

                # Train the discriminator
                D_real = self.discriminator(real_im, right_audio)
                D_wrong = self.discriminator(real_im, wrong_audio)
                D_fake = self.discriminator(fake_im.detach(), right_audio)

                loss_real = self.bce_loss_fn(D_real, self.ones)
                loss_wrong = self.bce_loss_fn(D_wrong, self.zeros)
                loss_fake = self.bce_loss_fn(D_fake, self.zeros)

                loss_disc = loss_real + 0.5 * (loss_fake + loss_wrong)

                loss_disc.backward()
                self.opt_d.step()
                self._reset_gradients()

                # Train the generator
                fake_im, audio_feature = self.generator(example, right_audio)
                D_fake = self.discriminator(fake_im, right_audio)

                f_audio = self.audio_encoder(audio_feature)
                f_images = self.images_encoder(real_im)

                avg_f_audio = F.avg_pool1d(f_audio, 16, 16).view(-1, 16)
                avg_f_images = F.avg_pool1d(f_images, 16, 16).view(-1, 16)

                loss_gen = self.bce_loss_fn(D_fake, self.ones)
                loss_cosine = self.cosine_loss_fn(avg_f_audio, avg_f_images, self.one_corr)

                loss_g = loss_gen + 0.5 * loss_cosine

                if config.fake_corr:
                    fake_f_images = self.images_encoder(fake_im)
                    fake_avg_f_images = F.avg_pool1d(fake_f_images, 16, 16).view(-1, 16)
                    loss_fake_cosine = self.cosine_loss_fn(avg_f_audio, fake_avg_f_images, self.one_corr)
                    loss_g += 0.5 * loss_fake_cosine

                loss_g.backward()

                self.opt_g.step()
                self.opt_corr.step()
                self._reset_gradients()

                t2 = time.time()

                if (step + 1) % 1 == 0 or (step + 1) == num_steps_per_epoch:
                    steps_remain = num_steps_per_epoch - step + 1 + \
                        (config.max_epochs - epoch + 1) * num_steps_per_epoch
                    eta = int((t2 - t1) * steps_remain)

                    if not config.fake_corr:
                        print("[{}/{}][{}/{}] Loss_D: {:.4f}  Loss_G: {:.4f}, cosine_loss: {}, ETA: {} second"
                          .format(epoch + 1, config.max_epochs,
                                  step + 1, num_steps_per_epoch,
                                  loss_disc.data[0], loss_gen.data[0], loss_cosine.data[0], eta))
                    else:
                        print("[{}/{}][{}/{}] Loss_D: {:.4f}  Loss_G: {:.4f}, cosine_loss: {}, fake_cosine_loss: {}, ETA: {} second"
                          .format(epoch + 1, config.max_epochs,
                                  step + 1, num_steps_per_epoch,
                                  loss_disc.data[0], loss_gen.data[0],
                                  loss_cosine.data[0], loss_fake_cosine.data[0], eta))
                            
                    log_value(
                        'discriminator_loss', loss_disc.data[0], step + num_steps_per_epoch * epoch)
                    log_value(
                        'generator_loss', loss_gen.data[0], step + num_steps_per_epoch * epoch)
                    log_value('cosine_loss', loss_cosine.data[0], step + num_steps_per_epoch * epoch)
                    if  config.fake_corr:
                        log_value('fake_cosine_loss', loss_fake_cosine.data[0], step + num_steps_per_epoch * epoch)
                        
                if (step) % (num_steps_per_epoch / 10) == 0:
                    fake_store = fake_im.data.permute(0, 2, 1, 3, 4).contiguous().view(
                        config.batch_size * 16, 3, 64, 64)
                    torchvision.utils.save_image(fake_store,
                                                 "{}fake_{}.png".format(config.sample_dir, cc), nrow=16, normalize=True)
                    real_store = real_im.data.permute(0, 2, 1, 3, 4).contiguous().view(
                        config.batch_size * 16, 3, 64, 64)
                    torchvision.utils.save_image(real_store,
                                                 "{}real_{}.png".format(config.sample_dir, cc), nrow=16, normalize=True)
                    cc += 1
            if epoch % 1 == 0:
                torch.save(self.generator.state_dict(),
                           "{}/generator_{}.pth"
                           .format(config.model_dir, epoch))
                torch.save(self.discriminator.state_dict(),
                           "{}/discriminator_{}.pth"
                           .format(config.model_dir, epoch))
                torch.save(self.audio_encoder.state_dict(),
                           "{}/audio_deri_encoder_{}.pth"
                           .format(config.model_dir, epoch))
                torch.save(self.images_encoder.state_dict(),
                           "{}/flow_encoder_{}.pth"
                           .format(config.model_dir, epoch))

    def load(self, directory):
        paths = glob.glob(os.path.join(directory, "*.pth"))
        gen_path = [path for path in paths if "generator" in path][0]
        disc_path = [path for path in paths if "discriminator" in path][0]
        flow_encoder_path = [path for path in paths if 'flow_encoder' in path][0]
        audio_deri_encoder_path = [path for path in paths if 'audio_deri_encoder' in path][0]

        self.generator.load_state_dict(torch.load(gen_path))
        self.discriminator.load_state_dict(torch.load(disc_path))
        self.audio_encoder.load_state_dict(torch.load(audio_deri_encoder_path))
        self.images_encoder.load_state_dict(torch.load(flow_encoder_path))

        self.start_epoch = int(gen_path.split(".")[0].split("_")[-1])
        print("Load pretrained [{}, {}]".format(gen_path, disc_path))

    def _reset_gradients(self):
        self.generator.zero_grad()
        self.discriminator.zero_grad()
        self.audio_encoder.zero_grad()
        self.images_encoder.zero_grad()

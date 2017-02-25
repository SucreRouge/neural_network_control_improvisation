#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Example employing Lasagne for piano roll generation
Crepe
https://github.com/Azure/Cortana-Intelligence-Gallery-Content/blob/master/Tutorials/Deep-Learning-for-Text-Classification-in-Azure/python/03%20-%20Crepe%20-%20Amazon%20(advc).py

Wasserstein Generative Adversarial Networks
(WGANs, see https://arxiv.org/abs/1701.07875 for the paper and
https://github.com/martinarjovsky/WassersteinGAN for the "official" code).

Adapted from Jan Schlüter's code
https://gist.github.com/f0k/f3190ebba6c53887d598d03119ca2066
"""

from __future__ import print_function
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from tqdm import tqdm

import time
import argparse

import numpy as np
np.random.seed(1234)
import theano
import theano.tensor as T
import lasagne
from models import build_generator, build_critic
from data_processing import load_text_data, encode_labels
import pdb

NOISE_SIZE = 256
CRITIC_ARCH = 2
GENERATOR_ARCH = 1


def iterate_minibatches(inputs, labels, batchsize, shuffle=True, forever=True,
                        length=128):
    from text_utils import binarizeText
    if shuffle:
        indices = np.arange(len(inputs))
    while True:
        if shuffle:
            np.random.shuffle(indices)
        for start_idx in range(0, len(inputs) - batchsize + 1, batchsize):
            if shuffle:
                excerpt = indices[start_idx:start_idx + batchsize]
            else:
                excerpt = slice(start_idx, start_idx + batchsize)

            if length > 0:
                data = []
                # select random slice from each piano roll
                for i in excerpt:
                    rand_start = np.random.randint(0, 1+len(inputs[i]) - length)
                    data.append(
                        binarizeText(inputs[i][rand_start:rand_start+length],
                                     remove_html=False))
            else:
                data = binarizeText(inputs[excerpt], remove_html=False)

            yield lasagne.utils.floatX(np.array(data)), labels[excerpt]

        if not forever:
            break


def main(num_epochs=100, epochsize=100, batchsize=128, initial_eta=2e-3,
         clip=0.01, boolean=False):
    # Load the dataset
    print("Loading data...")
    datapath = '/Users/rafaelvalle/Desktop/datasets/Text/ag_news_csv/train.csv'
    data_col, label_col = 2, 0
    n_pieces = 0  # 0 is equal to all pieces, unbalanced dataset
    as_dict = False
    inputs, labels = load_text_data(
        datapath, data_col, label_col, n_pieces, as_dict, patch_size=128)
    labels = encode_labels(labels, one_hot=True).astype(np.float32)

    print("Dataset shape {}".format(inputs.shape))
    epochsize = len(inputs) / batchsize

    # create fixed conditions and noise for output evaluation
    N_LABELS = labels.shape[1]
    N_SAMPLES = 12 * 12
    FIXED_CONDITION = lasagne.utils.floatX(np.eye(N_LABELS)[
        np.repeat(np.arange(N_LABELS), 1+(N_SAMPLES / N_LABELS))])
    FIXED_CONDITION = FIXED_CONDITION[:N_SAMPLES]
    FIXED_NOISE = lasagne.utils.floatX(np.random.rand(N_SAMPLES, NOISE_SIZE))

    # Prepare Theano variables for inputs
    noise_var = T.fmatrix('noise')
    cond_var = T.fmatrix('condition')
    input_var = T.ftensor4('inputs')

    # Create neural network model
    print("Building model and compiling functions...")
    generator = build_generator(
        noise_var, cond_var, labels.shape[1], NOISE_SIZE, GENERATOR_ARCH)
    crepe_critic = build_critic(input_var, CRITIC_ARCH)

    # Create expression for passing real data through the crepe_critic
    real_out = lasagne.layers.get_output(crepe_critic)
    # Create expression for passing fake data through the crepe_critic
    fake_out = lasagne.layers.get_output(
        crepe_critic, lasagne.layers.get_output(generator))

    # Create score expressions to be maximized (i.e., negative losses)
    generator_score = fake_out.mean()
    crepe_critic_score = real_out.mean() - fake_out.mean()

    # Create update expressions for training
    generator_params = lasagne.layers.get_all_params(generator, trainable=True)
    crepe_critic_params = lasagne.layers.get_all_params(crepe_critic, trainable=True)
    eta = theano.shared(lasagne.utils.floatX(initial_eta))
    generator_updates = lasagne.updates.rmsprop(
            -generator_score, generator_params, learning_rate=eta)
    crepe_critic_updates = lasagne.updates.rmsprop(
            -crepe_critic_score, crepe_critic_params, learning_rate=eta)

    # Clip crepe_critic parameters in a limited range around zero (except biases)
    for param in lasagne.layers.get_all_params(crepe_critic, trainable=True,
                                               regularizable=True):
        crepe_critic_updates[param] = T.clip(crepe_critic_updates[param], -clip, clip)

    # Instantiate a symbolic noise generator to use for training
    from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
    srng = RandomStreams(seed=np.random.randint(2147462579, size=6))
    noise = srng.uniform((batchsize, NOISE_SIZE))

    # Compile functions performing a training step on a mini-batch (according
    # to the updates dictionary) and returning the corresponding score:
    generator_train_fn = theano.function([cond_var],
                                         generator_score,
                                         givens={noise_var: noise},
                                         updates=generator_updates)
    crepe_critic_train_fn = theano.function([input_var, cond_var],
                                            crepe_critic_score,
                                            givens={noise_var: noise},
                                            updates=crepe_critic_updates)

    # Compile another function generating some data
    gen_fn = theano.function([noise_var, cond_var],
                             lasagne.layers.get_output(generator,
                                                       deterministic=True))

    # Finally, launch the training loop.
    print("Starting training...")
    # We create an infinite supply of batches (as an iterable generator):
    batches = iterate_minibatches(inputs, labels, batchsize, shuffle=True,
                                  length=128, forever=True)
    # We iterate over epochs:
    generator_updates = 0
    epoch_crepe_critic_scores = []
    epoch_generator_scores = []
    for epoch in range(num_epochs):
        start_time = time.time()

        # In each epoch, we do `epochsize` generator updates. Usually, the
        # crepe_critic is updated 5 times before every generator update. For the
        # first 25 generator updates and every 500 generator updates, the
        # crepe_critic is updated 100 times instead, following the authors' code.
        crepe_critic_scores = []
        generator_scores = []
        for _ in tqdm(range(epochsize)):
            if (generator_updates < 25) or (generator_updates % 500 == 0):
                crepe_critic_runs = 100
            else:
                crepe_critic_runs = 20 #5
            for _ in range(crepe_critic_runs):
                batch_in, batch_cond = next(batches)
                # scale to -1 and 1
                batch_in = batch_in * 2 - 1
                # reshape batch to proper dimensions
                batch_in = batch_in.reshape(
                    (batch_in.shape[0], 1, batch_in.shape[1], batch_in.shape[2]))
                crepe_critic_scores.append(crepe_critic_train_fn(batch_in, batch_cond))
            generator_scores.append(generator_train_fn(batch_cond))
            generator_updates += 1

        # Then we print the results for this epoch:
        print("Epoch {} of {} took {:.3f}s".format(
            epoch + 1, num_epochs, time.time() - start_time))
        epoch_crepe_critic_scores.append(np.mean(generator_scores))
        epoch_generator_scores.append(np.mean(crepe_critic_scores))

        fig, axes = plt.subplots(1, 2, figsize=(8, 2))
        axes[0].set_title('Loss(C)')
        axes[0].plot(epoch_crepe_critic_scores)
        axes[1].set_title('Loss(G)')
        axes[1].plot(epoch_generator_scores)
        fig.tight_layout()
        fig.savefig('images/wcgan_text/g_updates{}'.format(epoch))
        plt.close('all')

        # plot and create midi from generated data
        samples = gen_fn(FIXED_NOISE, FIXED_CONDITION)
        plt.imsave('images/wcgan_proll/wcgan_gits{}.png'.format(epoch),
                   (samples.reshape(12, 12, 128, 128)
                           .transpose(0, 2, 1, 3)
                           .reshape(12*128, 12*128)).T,
                   origin='bottom',
                   cmap='jet')
        np.save('text/wcgan_text/wcgan_gits{}.npy'.format(epoch), samples)

        # After half the epochs, we start decaying the learn rate towards zero
        if epoch >= num_epochs // 2:
            progress = float(epoch) / num_epochs
            eta.set_value(lasagne.utils.floatX(initial_eta*2*(1 - progress)))

        # After half the epochs, we start decaying the learn rate towards zero
        if epoch >= num_epochs // 2:
            progress = float(epoch) / num_epochs
            eta.set_value(lasagne.utils.floatX(initial_eta*2*(1 - progress)))

        if (epoch+1 % 50) == 0:
            np.savez('wcgan_text_gen_{}.npz'.format(epoch),
                     *lasagne.layers.get_all_param_values(generator))
            np.savez('wcgan_text_crit_{}.npz'.format(epoch),
                     *lasagne.layers.get_all_param_values(crepe_critic))
    #
    # And load them again later on like this:
    # with np.load('model.npz') as f:
    #     param_values = [f['arr_%d' % i] for i in range(len(f.files))]
    # lasagne.layers.set_all_param_values(network, param_values)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Trains WCGAN on Text data")
    parser.add_argument("-n", "--n_epochs", type=int, default=100,
                        help="Number of tepochs")
    parser.add_argument("-e", "--epoch_size", type=int, default=100,
                        help="Epoch Size")
    parser.add_argument("-m", "--bs", type=int, default=128,
                        help="Mini-Batch Size")
    parser.add_argument("-l", "--lr", type=float, default=2e-3,
                        help="Learning Rate")
    parser.add_argument("-c", "--clip", type=float, default=0.01,
                        help="Clip weights")
    parser.add_argument("-b", "--boolean", type=bool, default=0,
                        help="Data as boolean")

    args = parser.parse_args()
    print(args)
    main(args.n_epochs, args.epoch_size, args.bs, args.lr, args.clip,
         args.boolean)
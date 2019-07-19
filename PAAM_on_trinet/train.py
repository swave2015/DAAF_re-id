#!/usr/bin/env python3
from argparse import ArgumentParser
from datetime import timedelta
from importlib import import_module
import logging.config
import os
from signal import SIGINT, SIGTERM
import sys
import time

import json
import numpy as np
import tensorflow as tf
from tensorflow.contrib import slim
import re

import common
import lbtoolbox as lb
import loss
from nets import NET_CHOICES
from heads import HEAD_CHOICES
import heatmap
import keypoints

os.environ['CUDA_VISIBLE_DEVICES'] = '1'


def sample_k_fids_for_pid(pid, all_fids, all_pids, batch_k):
    """ Given a PID, select K FIDs of that specific PID. """
    possible_fids = tf.boolean_mask(all_fids, tf.equal(all_pids, pid))
    # 第一个参数为带处理的数据，第二个参数为 mask, mask 为true时的值保留，其余的除去。用 mask 去匹配前K个维度


    # The following simply uses a subset of K of the possible FIDs
    # if more than, or exactly K are available. Otherwise, we first
    # create a padded list of indices which contain a multiple of the
    # original FID count such that all of them will be sampled equally likely.
    count = tf.shape(possible_fids)[0]
    padded_count = tf.cast(tf.ceil(batch_k / tf.cast(count, tf.float32)), tf.int32) * count
    full_range = tf.mod(tf.range(padded_count), count)

    # Sampling is always performed by shuffling and taking the first k.
    shuffled = tf.random_shuffle(full_range)
    # 沿着要被洗牌的张量的第一个维度，随机打乱

    selected_fids = tf.gather(possible_fids, shuffled[:batch_k])

    return selected_fids, tf.fill([batch_k], pid)


def main():
    args = parser.parse_args()

    # We store all arguments in a json file. This has two advantages:
    # 1. We can always get back and see what exactly that experiment was
    # 2. We can resume an experiment as-is without needing to remember all flags.
    args_file = os.path.join(args.experiment_root, 'args.json')
    if args.resume:
        if not os.path.isfile(args_file):
            raise IOError('`args.json` not found in {}'.format(args_file))

        print('Loading args from {}.'.format(args_file))
        with open(args_file, 'r') as f:
            args_resumed = json.load(f)
        args_resumed['resume'] = True  # This would be overwritten.

        # When resuming, we not only want to populate the args object with the
        # values from the file, but we also want to check for some possible
        # conflicts between loaded and given arguments.
        for key, value in args.__dict__.items():
            if key in args_resumed:
                resumed_value = args_resumed[key]
                if resumed_value != value:
                    print('Warning: For the argument `{}` we are using the'
                          ' loaded value `{}`. The provided value was `{}`'
                          '.'.format(key, resumed_value, value))
                    args.__dict__[key] = resumed_value
            else:
                print('Warning: A new argument was added since the last run:'
                      ' `{}`. Using the new value: `{}`.'.format(key, value))

    else:
        # If the experiment directory exists already, we bail in fear.
        if os.path.exists(args.experiment_root):
            if os.listdir(args.experiment_root):
                print('The directory {} already exists and is not empty.'
                      ' If you want to resume training, append --resume to'
                      ' your call.'.format(args.experiment_root))
                exit(1)
        else:
            os.makedirs(args.experiment_root)

        # Store the passed arguments for later resuming and grepping in a nice
        # and readable format.
        with open(args_file, 'w') as f:
            json.dump(vars(args), f, ensure_ascii=False, indent=2, sort_keys=True)
            # json.dump用于将dict类型的数据转成str类型

    log_file = os.path.join(args.experiment_root, "train")
    logging.config.dictConfig(common.get_logging_dict(log_file))
    log = logging.getLogger('train')

    # Also show all parameter values at the start, for ease of reading logs.
    log.info('Training using the following parameters:')
    for key, value in sorted(vars(args).items()):
        log.info('{}: {}'.format(key, value))

    # Check them here, so they are not required when --resume-ing.
    if not args.train_set:
        parser.print_help()
        log.error("You did not specify the `train_set` argument!")
        sys.exit(1)
    if not args.image_root:
        parser.print_help()
        log.error("You did not specify the required `image_root` argument!")
        sys.exit(1)

    # Load the data from the CSV file.
    pids, fids = common.load_dataset(args.train_set, args.image_root, is_train=True)

    max_fid_len = max(map(len, fids))  # We'll need this later for logfiles
    # 值为31，即最大的字符串长度
    # map接受一个函数和一个list，将函数作用于list的每一个元素上并返回一个list
    # 返回 fid 中最长的字符串的长度

    # Setup a tf.Dataset where one "epoch" loops over all PIDS.
    # PIDS are shuffled after every epoch and continue indefinitely.

    unique_pids = np.unique(pids)
    # 对于一维数组或者列表，unique函数去除其中重复的元素，并按元素由小到大返回一个新的无元素重复的元组或者列表
    # 训练集中有741个人

    dataset = tf.data.Dataset.from_tensor_slices(unique_pids)
    # 切分形状上的第一个维度，形成数据集

    dataset = dataset.shuffle(len(unique_pids))
    # 对数据集进行打乱，参数为打乱时使用的 buffer 的大小


    # Constrain the dataset size to a multiple of the batch-size, so that
    # we don't get overlap at the end of each epoch.
    dataset = dataset.take((len(unique_pids) // args.batch_p) * args.batch_p)
    # take(count)  Creates a Dataset with at most count elements from this dataset.

    dataset = dataset.repeat(None)  # Repeat forever. Funny way of stating it.
    # Repeats this dataset count times.

    # For every PID, get K images.
    dataset = dataset.map(lambda pid: sample_k_fids_for_pid(
        pid, all_fids=fids, all_pids=pids, batch_k=args.batch_k))
    # map接收一个函数，Dataset中的每个元素都会被当作这个函数的输入，并将函数返回值作为新的 Dataset

    # Ungroup/flatten the batches for easy loading of the files.
    dataset = dataset.apply(tf.contrib.data.unbatch())
    # apply(transformation_func) Apply a transformation function to this dataset.
    # apply enables chaining of custom Dataset transformations, which are represented as functions that take one Dataset argument and return a transformed Dataset.


    # Convert filenames to actual image tensors.
    net_input_size = (args.net_input_height, args.net_input_width)
    # 256，128
    pre_crop_size = (args.pre_crop_height, args.pre_crop_width)
    # 288，144
    
    dataset = dataset.map(
        lambda fid, pid: common.fid_to_image_label(
            fid, pid, image_root=args.image_root,
            image_size=pre_crop_size if args.crop_augment else net_input_size),
        num_parallel_calls=args.loading_threads)


    # merge
###########################################################################################
    dataset = dataset.map(
        lambda im, keypt, mask, fid, pid: (tf.concat([im, keypt, mask], 2), fid, pid))

    # 将图片和热度图一一对应
###########################################################################################
    
    # Augment the data if specified by the arguments.
    if args.flip_augment:
        dataset = dataset.map(
            lambda im, fid, pid: (tf.image.random_flip_left_right(im), fid, pid))


    # net_input_size_aug = net_input_size + (4,)
    if args.crop_augment:
        dataset = dataset.map(
            lambda im, fid, pid: (tf.random_crop(im, net_input_size + (21,)), fid, pid))
    # net_input_size + (21,) = (256, 128, 21)
    # split

#############################################################################################
    dataset = dataset.map(
        lambda im, fid, pid: (common.split(im, fid, pid)))
    # 将热度图和训练图片分开

#############################################################################################

    # Group it back into PK batches.
    batch_size = args.batch_p * args.batch_k
    dataset = dataset.batch(batch_size)

    # Overlap producing and consuming for parallelism.
    dataset = dataset.prefetch(1)
    # prefetch(buffer_size)   Creates a Dataset that prefetches elements from this dataset.

    # Since we repeat the data infinitely, we only need a one-shot iterator.
    images, keypts, masks, fids, pids = dataset.make_one_shot_iterator().get_next()
    # tf.summary.image('image',images,10)
    # 创建一个迭代器遍历数据集中的所有元素

    # Create the model and an embedding head.
    model = import_module('nets.' + args.model_name)
    head = import_module('heads.' + args.head_name)
    # 加载模型，结构是预先定义好的，如 resnet50

    # Feed the image through the model. The returned `body_prefix` will be used
    # further down to load the pre-trained weights for all variables with this
    # prefix.

    endpoints, body_prefix = model.endpoints(images, is_training=True)
    heatmap_in = endpoints[args.model_name + '/block4']
    # resnet_block_4_out = heatmap.resnet_block_4(heatmap_in)
    # resnet_block_3_4_out = heatmap.resnet_block_3_4(heatmap_in)
    # resnet_block_2_3_4_out = heatmap.resnet_block_2_3_4(heatmap_in)
    # head for heatmap
    with tf.name_scope('heatmap'):
        # heatmap_in = endpoints['model_output']
        # heatmap_out_layer_0 = heatmap.hmnet_layer_0(resnet_block_4_out, 1)
        # heatmap_out_layer_0 = heatmap.hmnet_layer_0(resnet_block_3_4_out, 1)
        # heatmap_out_layer_0 = heatmap.hmnet_layer_0(resnet_block_2_3_4_out, 1)
        heatmap_out_layer_0 = heatmap.hmnet_layer_0(heatmap_in[:, :, :, 1020:2048], 1)
        heatmap_out_layer_1 = heatmap.hmnet_layer_1(heatmap_out_layer_0, 1)
        heatmap_out_layer_2 = heatmap.hmnet_layer_2(heatmap_out_layer_1, 1)
        heatmap_out_layer_3 = heatmap.hmnet_layer_3(heatmap_out_layer_2, 1)
        heatmap_out_layer_4 = heatmap.hmnet_layer_4(heatmap_out_layer_3, 1)
        heatmap_out = heatmap_out_layer_4
        heatmap_loss = heatmap.loss_mutilayer(heatmap_out_layer_0, heatmap_out_layer_1, heatmap_out_layer_2,
                                              heatmap_out_layer_3, heatmap_out_layer_4, masks, net_input_size)
        # heatmap_loss = heatmap.loss(heatmap_out, labels, net_input_size)
        # heatmap_loss_mean = heatmap_loss

    with tf.name_scope('head'):
        # heatmap_sum = tf.reduce_sum(heatmap_out, axis=3)
        # heatmap_resize = tf.image.resize_images(tf.expand_dims(heatmap_sum, axis=3), [8, 4])
        # featuremap_tmp = tf.multiply(heatmap_resize, endpoints[args.model_name + '/block4'])
        # endpoints[args.model_name + '/block4'] = featuremap_tmp
        endpoints = head.head(endpoints, args.embedding_dim, is_training=True)

        tf.summary.image('feature_map', tf.expand_dims(endpoints[args.model_name + '/block4'][:, :, :, 0], axis=3), 4)


    with tf.name_scope('keypoints_pre'):
        keypoints_pre_in = endpoints[args.model_name + '/block4']
        # keypoints_pre_in_0 = keypoints_pre_in[:, :, :, 0:256]
        # keypoints_pre_in_1 = keypoints_pre_in[:, :, :, 256:512]
        # keypoints_pre_in_2 = keypoints_pre_in[:, :, :, 512:768]
        # keypoints_pre_in_3 = keypoints_pre_in[:, :, :, 768:1024]
        keypoints_pre_in_0 = keypoints_pre_in[:, :, :, 0:170]
        keypoints_pre_in_1 = keypoints_pre_in[:, :, :, 170:340]
        keypoints_pre_in_2 = keypoints_pre_in[:, :, :, 340:510]
        keypoints_pre_in_3 = keypoints_pre_in[:, :, :, 510:680]
        keypoints_pre_in_4 = keypoints_pre_in[:, :, :, 680:850]
        keypoints_pre_in_5 = keypoints_pre_in[:, :, :, 850:1020]

        labels = tf.image.resize_images(keypts, [128, 64])
        # keypoints_gt_0 = tf.concat([labels[:, :, :, 0:5], labels[:, :, :, 14:15], labels[:, :, :, 15:16], labels[:, :, :, 16:17], labels[:, :, :, 17:18]], 3)
        # keypoints_gt_1 = tf.concat([labels[:, :, :, 1:2], labels[:, :, :, 2:3], labels[:, :, :, 3:4], labels[:, :, :, 5:6]], 3)
        # keypoints_gt_2 = tf.concat([labels[:, :, :, 4:5], labels[:, :, :, 7:8], labels[:, :, :, 8:9], labels[:, :, :, 11:12]], 3)
        # keypoints_gt_3 = tf.concat([labels[:, :, :, 9:10], labels[:, :, :, 10:11], labels[:, :, :, 12:13], labels[:, :, :, 13:14]], 3)

        keypoints_gt_0 = labels[:, :, :, 0:5]
        keypoints_gt_1 = labels[:, :, :, 5:7]
        keypoints_gt_2 = labels[:, :, :, 7:9]
        keypoints_gt_3 = labels[:, :, :, 9:13]
        keypoints_gt_4 = labels[:, :, :, 13:15]
        keypoints_gt_5 = labels[:, :, :, 15:17]

        keypoints_pre_0 = keypoints.tran_conv_0(keypoints_pre_in, kp_num=5)
        keypoints_pre_1 = keypoints.tran_conv_1(keypoints_pre_in, kp_num=2)
        keypoints_pre_2 = keypoints.tran_conv_2(keypoints_pre_in, kp_num=2)
        keypoints_pre_3 = keypoints.tran_conv_3(keypoints_pre_in, kp_num=4)
        keypoints_pre_4 = keypoints.tran_conv_4(keypoints_pre_in, kp_num=2)
        keypoints_pre_5 = keypoints.tran_conv_5(keypoints_pre_in, kp_num=2)

        keypoints_loss_0 = keypoints.keypoints_loss(keypoints_pre_0, keypoints_gt_0)
        keypoints_loss_1 = keypoints.keypoints_loss(keypoints_pre_1, keypoints_gt_1)
        keypoints_loss_2 = keypoints.keypoints_loss(keypoints_pre_2, keypoints_gt_2)
        keypoints_loss_3 = keypoints.keypoints_loss(keypoints_pre_3, keypoints_gt_3)
        keypoints_loss_4 = keypoints.keypoints_loss(keypoints_pre_4, keypoints_gt_4)
        keypoints_loss_5 = keypoints.keypoints_loss(keypoints_pre_5, keypoints_gt_5)

        keypoints_loss = 5/17*keypoints_loss_0 + 2/17*keypoints_loss_1 + 2/17*keypoints_loss_2 + 4/17*keypoints_loss_3 + 2/17*keypoints_loss_4 + 2/17*keypoints_loss_5


    # Create the loss in two steps:
    # 1. Compute all pairwise distances according to the specified metric.
    # 2. For each anchor along the first dimension, compute its loss.
    dists = loss.cdist(endpoints['emb'], endpoints['emb'], metric=args.metric)
    losses, train_top1, prec_at_k, _, neg_dists, pos_dists = loss.LOSS_CHOICES[args.loss](
        dists, pids, args.margin, batch_precision_at_k=args.batch_k-1)

    # Count the number of active entries, and compute the total batch loss.
    num_active = tf.reduce_sum(tf.cast(tf.greater(losses, 1e-5), tf.float32))
    loss_mean = tf.reduce_mean(losses)
    
    scale_rate_0 = 1E-7
    scale_rate_1 = 6E-8
    total_loss = loss_mean + keypoints_loss*scale_rate_0 + heatmap_loss*scale_rate_1
    # total_loss = loss_mean + keypoints_loss * scale_rate_0
    # total_loss = loss_mean

    # Some logging for tensorboard.
    tf.summary.histogram('loss_distribution', losses)
    tf.summary.scalar('loss', loss_mean)
############################################################################################
    # tf.summary.histogram('hm_loss_distribution', heatmap_loss)
    tf.summary.scalar('keypt_loss_0', keypoints_loss_0)
    tf.summary.scalar('keypt_loss_1', keypoints_loss_1)
    tf.summary.scalar('keypt_loss_2', keypoints_loss_2)
    tf.summary.scalar('keypt_loss_3', keypoints_loss_3)
    tf.summary.scalar('keypt_loss_all', keypoints_loss)
############################################################################################
    tf.summary.scalar('total_loss', total_loss)
    tf.summary.scalar('batch_top1', train_top1)
    tf.summary.scalar('batch_prec_at_{}'.format(args.batch_k-1), prec_at_k)
    tf.summary.scalar('active_count', num_active)
    tf.summary.histogram('embedding_dists', dists)
    tf.summary.histogram('embedding_pos_dists', pos_dists)
    tf.summary.histogram('embedding_neg_dists', neg_dists)
    tf.summary.histogram('embedding_lengths',
                         tf.norm(endpoints['emb_raw'], axis=1))

    # Create the mem-mapped arrays in which we'll log all training detail in
    # addition to tensorboard, because tensorboard is annoying for detailed
    # inspection and actually discards data in histogram summaries.
    if args.detailed_logs:
        log_embs = lb.create_or_resize_dat(
            os.path.join(args.experiment_root, 'embeddings'),
            dtype=np.float32, shape=(args.train_iterations, batch_size, args.embedding_dim))
        log_loss = lb.create_or_resize_dat(
            os.path.join(args.experiment_root, 'losses'),
            dtype=np.float32, shape=(args.train_iterations, batch_size))
        log_fids = lb.create_or_resize_dat(
            os.path.join(args.experiment_root, 'fids'),
            dtype='S' + str(max_fid_len), shape=(args.train_iterations, batch_size))

    # These are collected here before we add the optimizer, because depending
    # on the optimizer, it might add extra slots, which are also global
    # variables, with the exact same prefix.
    model_variables = tf.get_collection(
        tf.GraphKeys.GLOBAL_VARIABLES, body_prefix)

    # Define the optimizer and the learning-rate schedule.
    # Unfortunately, we get NaNs if we don't handle no-decay separately.
    global_step = tf.Variable(0, name='global_step', trainable=False)
    if 0 <= args.decay_start_iteration < args.train_iterations:
        learning_rate = tf.train.exponential_decay(
            args.learning_rate,
            tf.maximum(0, global_step - args.decay_start_iteration),
            args.train_iterations - args.decay_start_iteration, 0.001)
    else:
        learning_rate = args.learning_rate
    tf.summary.scalar('learning_rate', learning_rate)
    optimizer = tf.train.AdamOptimizer(learning_rate)
    # Feel free to try others!
    # optimizer = tf.train.AdadeltaOptimizer(learning_rate)

    # Update_ops are used to update batchnorm stats.
    with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
    #    train_op = optimizer.minimize(loss_mean, global_step=global_step)
        train_op = optimizer.minimize(total_loss, global_step=global_step)
    #


    # Define a saver for the complete model.
    checkpoint_saver = tf.train.Saver(max_to_keep=0)


    gpu_options = tf.GPUOptions(allow_growth=True)
    with tf.Session(config=tf.ConfigProto(gpu_options=gpu_options)) as sess:
        if args.resume:
            # In case we're resuming, simply load the full checkpoint to init.
            last_checkpoint = tf.train.latest_checkpoint(args.experiment_root)
            log.info('Restoring from checkpoint: {}'.format(last_checkpoint))
            checkpoint_saver.restore(sess, last_checkpoint)
        else:
            # But if we're starting from scratch, we may need to load some
            # variables from the pre-trained weights, and random init others.
            sess.run(tf.global_variables_initializer())
            if args.initial_checkpoint is not None:
                saver = tf.train.Saver(model_variables, write_version=tf.train.SaverDef.V1)
                
                saver.restore(sess, args.initial_checkpoint)

                # name_11 = 'resnet_v1_50/block4'
                # name_12 = 'resnet_v1_50/block3'
                # name_13 = 'resnet_v1_50/block2'
                # name_21 = 'Resnet_block_2_3_4/block4'
                # name_22 = 'Resnet_block_2_3_4/block3'
                # name_23 = 'Resnet_block_2_3_4/block2'
                # for var in tf.trainable_variables():
                #     var_name = var.name
                #     if re.match(name_11, var_name):
                #         dst_name = var_name.replace(name_11, name_21)
                #         tensor = tf.get_default_graph().get_tensor_by_name(var_name)
                #         dst_tensor = tf.get_default_graph().get_tensor_by_name(dst_name)
                #         tf.assign(dst_tensor, tensor)
                #     if re.match(name_12, var_name):
                #         dst_name = var_name.replace(name_12, name_22)
                #         tensor = tf.get_default_graph().get_tensor_by_name(var_name)
                #         dst_tensor = tf.get_default_graph().get_tensor_by_name(dst_name)
                #         tf.assign(dst_tensor, tensor)
                #     if re.match(name_13, var_name):
                #         dst_name = var_name.replace(name_13, name_23)
                #         tensor = tf.get_default_graph().get_tensor_by_name(var_name)
                #         dst_tensor = tf.get_default_graph().get_tensor_by_name(dst_name)
                #         tf.assign(dst_tensor, tensor)
            # In any case, we also store this initialization as a checkpoint,
            # such that we could run exactly reproduceable experiments.
            checkpoint_saver.save(sess, os.path.join(
                args.experiment_root, 'checkpoint'), global_step=0)

        merged_summary = tf.summary.merge_all()
        summary_writer = tf.summary.FileWriter(args.experiment_root, sess.graph)

        start_step = sess.run(global_step)
        log.info('Starting training from iteration {}.'.format(start_step))

        # Finally, here comes the main-loop. This `Uninterrupt` is a handy
        # utility such that an iteration still finishes on Ctrl+C and we can
        # stop the training cleanly.
        with lb.Uninterrupt(sigs=[SIGINT, SIGTERM], verbose=True) as u:
            for i in range(start_step, args.train_iterations):

                # Compute gradients, update weights, store logs!
                start_time = time.time()
                _, summary, step, b_prec_at_k, b_embs, b_loss, b_fids = \
                    sess.run([train_op, merged_summary, global_step,
                              prec_at_k, endpoints['emb'], losses, fids])
                elapsed_time = time.time() - start_time

                # Compute the iteration speed and add it to the summary.
                # We did observe some weird spikes that we couldn't track down.
                summary2 = tf.Summary()
                summary2.value.add(tag='secs_per_iter', simple_value=elapsed_time)
                summary_writer.add_summary(summary2, step)
                summary_writer.add_summary(summary, step)

                if args.detailed_logs:
                    log_embs[i], log_loss[i], log_fids[i] = b_embs, b_loss, b_fids

                # Do a huge print out of the current progress.
                seconds_todo = (args.train_iterations - step) * elapsed_time
                log.info('iter:{:6d}, loss min|avg|max: {:.3f}|{:.3f}|{:6.3f}, '
                         'batch-p@{}: {:.2%}, ETA: {} ({:.2f}s/it)'.format(
                             step,
                             float(np.min(b_loss)),
                             float(np.mean(b_loss)),
                             float(np.max(b_loss)),
                             args.batch_k-1, float(b_prec_at_k),
                             timedelta(seconds=int(seconds_todo)),
                             elapsed_time))
                sys.stdout.flush()
                sys.stderr.flush()

                # Save a checkpoint of training every so often.
                if (args.checkpoint_frequency > 0 and
                        step % args.checkpoint_frequency == 0):
                    checkpoint_saver.save(sess, os.path.join(
                        args.experiment_root, 'checkpoint'), global_step=step)

                # Stop the main-loop at the end of the step, if requested.
                if u.interrupted:
                    log.info("Interrupted on request!")
                    break

        # Store one final checkpoint. This might be redundant, but it is crucial
        # in case intermediate storing was disabled and it saves a checkpoint
        # when the process was interrupted.
        checkpoint_saver.save(sess, os.path.join(
            args.experiment_root, 'checkpoint'), global_step=step)


if __name__ == '__main__':
    parser = ArgumentParser(description='Train a ReID network.')

    # Required.

    parser.add_argument(
        '--experiment_root', required=True, type=common.writeable_directory,
        help='Location used to store checkpoints and dumped data.')

    parser.add_argument(
        '--train_set',
        help='Path to the train_set csv file.')

    parser.add_argument(
        '--image_root', type=common.readable_directory,
        help='Path that will be pre-pended to the filenames in the train_set csv.')

    # Optional with sane defaults.

    parser.add_argument(
        '--resume', action='store_true', default=False,
        help='When this flag is provided, all other arguments apart from the '
             'experiment_root are ignored and a previously saved set of arguments '
             'is loaded.')

    parser.add_argument(
        '--model_name', default='resnet_v1_50', choices=NET_CHOICES,
        help='Name of the model to use.')

    parser.add_argument(
        '--head_name', default='fc1024', choices=HEAD_CHOICES,
        help='Name of the head to use.')

    parser.add_argument(
        '--embedding_dim', default=128, type=common.positive_int,
        help='Dimensionality of the embedding space.')

    parser.add_argument(
        '--initial_checkpoint', default=None,
        help='Path to the checkpoint file of the pretrained network.')

    # TODO move these defaults to the .sh script?
    parser.add_argument(
        '--batch_p', default=16, type=common.positive_int,
        help='The number P used in the PK-batches')

    parser.add_argument(
        '--batch_k', default=4, type=common.positive_int,
        help='The numberK used in the PK-batches')

    parser.add_argument(
        '--net_input_height', default=256, type=common.positive_int,
        help='Height of the input directly fed into the network.')

    parser.add_argument(
        '--net_input_width', default=128, type=common.positive_int,
        help='Width of the input directly fed into the network.')

    parser.add_argument(
        '--pre_crop_height', default=288, type=common.positive_int,
        help='Height used to resize a loaded image. This is ignored when no crop '
             'augmentation is applied.')

    parser.add_argument(
        '--pre_crop_width', default=144, type=common.positive_int,
        help='Width used to resize a loaded image. This is ignored when no crop '
             'augmentation is applied.')
    # TODO end

    parser.add_argument(
        '--loading_threads', default=16, type=common.positive_int,
        help='Number of threads used for parallel loading.')

    parser.add_argument(
        '--margin', default='soft', type=common.float_or_string,
        help='What margin to use: a float value for hard-margin, "soft" for '
             'soft-margin, or no margin if "none".')

    parser.add_argument(
        '--metric', default='euclidean', choices=loss.cdist.supported_metrics,
        help='Which metric to use for the distance between embeddings.')

    parser.add_argument(
        '--loss', default='batch_hard', choices=loss.LOSS_CHOICES.keys(),
        help='Enable the super-mega-advanced top-secret sampling stabilizer.')

    parser.add_argument(
        '--learning_rate', default=1e-4, type=common.positive_float,
        help='The initial value of the learning-rate, before it kicks in.')

    parser.add_argument(
        '--train_iterations', default=80000, type=common.positive_int,
        help='Number of training iterations.')

    parser.add_argument(
        '--decay_start_iteration', default=15000, type=int,
        help='At which iteration the learning-rate decay should kick-in.'
             'Set to -1 to disable decay completely.')

    parser.add_argument(
        '--checkpoint_frequency', default=10000, type=common.nonnegative_int,
        help='After how many iterations a checkpoint is stored. Set this to 0 to '
             'disable intermediate storing. This will result in only one final '
             'checkpoint.')

    parser.add_argument(
        '--flip_augment', action='store_true', default=False,
        help='When this flag is provided, flip augmentation is performed.')

    parser.add_argument(
        '--crop_augment', action='store_true', default=False,
        help='When this flag is provided, crop augmentation is performed. Based on'
             'The `crop_height` and `crop_width` parameters. Changing this flag '
             'thus likely changes the network input size!')

    parser.add_argument(
        '--detailed_logs', action='store_true', default=False,
        help='Store very detailed logs of the training in addition to TensorBoard'
             ' summaries. These are mem-mapped numpy files containing the'
             ' embeddings, losses and FIDs seen in each batch during training.'
             ' Everything can be re-constructed and analyzed that way.')
    main()

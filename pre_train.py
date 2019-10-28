# coding:utf-8
# Produced by Andysin Zhang
# 23_Oct_2019
# Inspired By the original Bert, Appreciate for the wonderful work
#
# Copyright 2019 TCL Inc. All Rights Reserverd.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

""""Run masked LM/next sentence masked_lm pre-training for ALBERT."""

import sys
import functools
import tensorflow as tf
# tf.enable_eager_execution()

from pathlib import Path
PROJECT_PATH = Path(__file__).absolute().parent
sys.path.insert(0, str(PROJECT_PATH))

from utils.setup import Setup
setup = Setup()

from model import BertModel
from model_helper import *
from config import bert_config
from load_data import train_input_fn
from utils.log import log_info as _info
from utils.log import log_error as _error


# Prototype for tf.estimator
def model_fn_builder(bert_config, init_checkpoint, learning_rate, num_train_steps):
    """Returns 'model_fn' closure for Estomator,
       use closure is because of building the model requires
       some paramters, sending them into the 'params' is not a good deal."""

    def model_fn(features, labels, mode, params):
        """this is prototype syntax, all parameters are necessary."""
        # obtain the data
        _info('*** Features ***')
        for name in sorted(features.keys()):
            tf.logging.info("  name = %s, shape = %s" % (name, features[name].shape))

        input_ids = features['input_ids']       # [batch_size, seq_length]
        input_mask = features['input_mask']     # [batch_size, seq_length]
        # segment_idx = features['segment_dis']
        masked_lm_positions = features['masked_lm_positions']   # [batch_size, seq_length], specify the answer
        masked_lm_ids = features['masked_lm_ids']               # [batch_size, answer_seq_length], specify the answer labels
        masked_lm_weights = features['masked_lm_weights']        # [batch_size, seq_length], [1, 1, 0], 0 refers to the mask
        # next_sentence_labels = features['next_sentence_labels']

        # build model
        is_training = (mode == tf.estimator.ModeKeys.TRAIN)
        model = BertModel(
            config=bert_config,
            is_training=is_training,
            input_ids=input_ids,
            input_mask=input_mask)

        # compute loss
        loss, pre_loss, log_probs = get_masked_lm_output(bert_config,
                                                         model.get_sequence_output(),
                                                         model.embedding_table,
                                                         model.projection_table,
                                                         masked_lm_positions,
                                                         masked_lm_ids,
                                                         masked_lm_weights)
        
        # restore from the checkpoint,
        # tf.estimator automatically restore from the model typically,
        # maybe here is for restore some pre-trained parameters
        tvars = tf.trainable_variables()
        initialized_variable_names = {}
        if init_checkpoint:
            (assignment_map, initialized_variable_names) = get_assignment_map_from_checkpoint(tvars, init_checkpoint)
            tf.train.init_from_checkpoint(init_checkpoint, assignment_map)

        _info('*** Trainable Variables ***')
        for var in tvars:
            init_string = ''
            if var.name in initialized_variable_names:
                init_string = ', *INIT_FROM_CKPT*'
            _info('name = {}, shape={}{}'.format(var.name, var.shape, init_string))
        
        if mode == tf.estimator.ModeKeys.TRAIN:
            learning_rate = tf.train.polynomial_decay(bert_config.learning_rate,
                                                      tf.train.get_or_create_global_step(),
                                                      num_train_steps,
                                                      end_learning_rate=0.0,
                                                      power=1.0,
                                                      cycle=False)
            optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)
            gradients = tf.gradients(loss, tvars, colocate_gradients_with_ops=True)
            clipped_gradients, _ = tf.clip_by_global_norm(gradients, 5.0)
            train_op = optimizer.apply_gradients(zip(clipped_gradients, tvars), global_step=tf.train.get_global_step())
            output_spec = tf.estimator.EstimatorSpec(mode, loss=loss, train_op=train_op)
        elif mode == tf.estimator.ModeKeys.EVAL:
            # TODO define the metrics
            _error('to do ...')
            raise NotImplementedError
        elif mode == tf.estimator.ModeKeys.PREDICT:
            masked_lm_predictions = tf.argmax(log_probs, axis=-1, output_type=tf.int32)
            output_spec = tf.estimator.EstimatorSpec(mode, predictions=masked_lm_predictions)

        return output_spec
    
    return model_fn

def get_masked_lm_output(bert_config, input_tensor, embedding_table, projection_table, positions, 
                         label_ids, label_weights):
    """Get the loss for the answer according to the mask.
    
    Args:
        bert_config: config for bert.
        input_tensor: float Tensor of shape [batch_size, seq_length, witdh].
        embedding_table: [vocab_size, embedding_size].
        projection_table: [embedding_size, hidden_size].
        positions: tf.int32, which saves the positions for answers.
        label_ids: tf.int32, which is the true labels.
        label_weights: tf.int32, which is refers to the padding.
    
    Returns:
        loss: average word loss.
        per_loss: per word loss.
        log_probs: log probability.
    """
    predicted_tensor = gather_indexes(input_tensor, positions)

    with tf.variable_scope('seq2seq/predictions'):
        with tf.variable_scope('transform'):
            input_tensor = tf.layers.dense(
                predicted_tensor,
                units=bert_config.hidden_size,
                activation=gelu,
                kernel_initializer=create_initializer(bert_config.initializer_range))
        input_tensor = layer_norm(input_tensor)

        output_bias = tf.get_variable(
            'output_bias',
            shape=[bert_config.vocab_size],
            initializer=tf.zeros_initializer())
        input_project = tf.matmul(input_tensor, projection_table, transpose_b=True)
        logits = tf.matmul(input_project, embedding_table, transpose_b=True)
        logits = tf.nn.bias_add(logits, output_bias)
        # [some_length, vocab_size]
        log_probs = tf.nn.log_softmax(logits, axis=-1)

        # [some_length], no need to cast to tf.float32
        label_ids = tf.reshape(label_ids, [-1])
        # [some_length]
        label_weights = tf.cast(tf.reshape(label_ids, [-1]), dtype=tf.float32)

        # [some_length, vocab_size]
        one_hot_labels = tf.one_hot(label_ids, depth=bert_config.vocab_size)
        # [some_length, 1]
        per_loss = - tf.reduce_sum(log_probs * one_hot_labels, axis=-1)
        # ignore padding
        numerator = tf.reduce_sum(label_weights * per_loss)
        # the number of predicted items
        denominator = tf.reduce_sum(label_weights) + 1e-5
        loss = numerator / denominator
    
    return loss, per_loss, log_probs

def gather_indexes(input_tensor, positions):
    """Gather all the predicted tensor, input_tensor contains all the positions,
        however, only maksed positions are used for calculating the loss.
    
    Args:
        input_tensor: float Tensor of shape [batch_size, seq_length, width].
        positions: save the relative positions of each sentence's labels.
    
    Returns:
        output_tensor: [some_length, width], where some_length refers to all the predicted labels
            in the data batch.
        """
    input_shape = get_shape_list(input_tensor, expected_rank=3)
    batch_size = input_shape[0]
    seq_length = input_shape[1]
    width = input_shape[2]

    # create a vector which saves the initial positions for each batch
    flat_offsets = tf.reshape(
        tf.range(0, batch_size, dtype=tf.int32) * seq_length, [-1, 1])
    # get the absolute positions for the predicted labels, [batch_size * seq_length, 1]
    flat_postions = tf.reshape(positions + flat_offsets, [-1])
    flat_input_tensor = tf.reshape(input_tensor,
                                    [batch_size * seq_length, width])
    # obtain the predicted items, [some_lenght, width]
    output_tensor = tf.gather(flat_input_tensor, flat_postions)
    
    return output_tensor

def main():
    # tf.gfile.MakeDirs(FLAGS.output_dir)
    Path(bert_config.model_dir).mkdir(exist_ok=True)

    model_fn = model_fn_builder(
        bert_config=bert_config,
        init_checkpoint=bert_config.init_checkpoint,
        learning_rate=bert_config.learning_rate,
        num_train_steps=bert_config.num_train_steps)
    
    input_fn = functools.partial(train_input_fn, 
                                 path=bert_config.data_path,
                                 batch_size=bert_config.batch_size,
                                 repeat_num=bert_config.num_train_steps)

    run_config = tf.contrib.tpu.RunConfig(
        keep_checkpoint_max=5,
        save_checkpoints_steps=5,
        model_dir=bert_config.model_dir)
    estimator = tf.estimator.Estimator(model_fn, config=run_config)
    estimator.train(input_fn, steps=bert_config.num_train_steps)

if __name__ == '__main__':
    main()
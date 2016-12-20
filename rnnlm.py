import argparse
import bz2
import code
import collections
import logging
import numpy as np
import os
import pandas
import random
import tensorflow as tf

from vocab import Vocab
from batcher import Dataset
from model2 import HyperModel, StandardModel


parser = argparse.ArgumentParser()
parser.add_argument('expdir')
parser.add_argument('--mode', choices=['train', 'debug', 'eval'],
                    default='train')
parser.add_argument('--model', choices=['multi', 'bias'],
                    default='bias')
parser.add_argument('--fancy_bias', default=False)
parser.add_argument('--dataset', default='all')
args = parser.parse_args()

if not os.path.exists(args.expdir):
  os.mkdir(args.expdir)

config = tf.ConfigProto(inter_op_parallelism_threads=10,
                        intra_op_parallelism_threads=10)

def ReadData(filename, limit=2500000):
  usernames = []
  texts = []

  with bz2.BZ2File(filename, 'r') as f:
    for idnum, line in enumerate(f):
      username, text = line.split('\t')

      if idnum % 30000 == 0:
        print idnum

      if idnum > limit:
        break

      if args.mode == 'train' and int(idnum) % 10 < 1:
        continue
      if args.mode != 'train' and int(idnum) % 10 >= 1:
        continue

      usernames.append(username)
      texts.append(['<S>'] + text.lower().split() + ['</S>'])

  return usernames, texts

filename = '/s0/ajaech/clean.tsv.bz'
usernames, texts = ReadData(filename)
max_len = 36

dataset = Dataset(max_len=max_len, preshuffle=True)
dataset.AddDataSource(usernames, texts)

if args.mode == 'train':
  vocab = Vocab.MakeFromData(texts, min_count=20)
  username_vocab = Vocab.MakeFromData([[u] for u in usernames],
                                      min_count=50)
  vocab.Save(os.path.join(args.expdir, 'word_vocab.pickle'))
  username_vocab.Save(os.path.join(args.expdir, 'username_vocab.pickle'))
  print 'num users {0}'.format(len(username_vocab))
  print 'vocab size {0}'.format(len(vocab))
else:
  vocab = Vocab.Load(os.path.join(args.expdir, 'word_vocab.pickle'))
  username_vocab = Vocab.Load(os.path.join(args.expdir, 'username_vocab.pickle'))

if args.mode != 'debug':
  dataset.Prepare(vocab, username_vocab)

model = StandardModel(max_len-1, len(vocab), use_nce_loss=False)
# model = HyperModel(max_len-1, len(vocab), len(username_vocab), use_nce_loss=False)

saver = tf.train.Saver(tf.all_variables())
session = tf.Session(config=config)

def Train(expdir):
  logging.basicConfig(filename=os.path.join(expdir, 'logfile.txt'),
                      level=logging.INFO)
  tvars = tf.trainable_variables()
  grads, _ = tf.clip_by_global_norm(tf.gradients(model.cost, tvars), 5.0)
  optimizer = tf.train.AdamOptimizer(0.001)
  train_op = optimizer.apply_gradients(zip(grads, tvars))

  print('initalizing')
  session.run(tf.initialize_all_variables())

  for idx in xrange(800000):
    s, seq_len, usernames = dataset.GetNextBatch()

    feed_dict = {
      model.x: s[:, :-1],
      model.y: s[:, 1:],
      model.seq_len: seq_len,
      # model.username: usernames
    }

    a = session.run([model.cost, train_op], feed_dict)
  
    if idx % 25 == 0:
      ws = [vocab.idx_to_word[s[0, i]] for i in range(seq_len[0])]
      print ' '.join(ws)
      print float(a[0])
      print '-------'
      logging.info({'iter': idx, 'cost': float(a[0])})

      if idx % 1000 == 0:
        saver.save(session, os.path.join(expdir, 'model.bin'))


def Greedy(expdir):
  saver.restore(session, os.path.join(expdir, 'model.bin'))

  current_word = '<S>'
  prevstate_h = np.zeros((1, 150))
  prevstate_c = np.zeros((1, 150))

  for i in xrange(10):

    a = session.run([model.next_word_prob, model.nextstate_h, model.nextstate_c],
                    {model.wordid: np.array([vocab[current_word]]),
                     model.prevstate_c: prevstate_c,
                     model.prevstate_h: prevstate_h
                     })
    probs, prevstate_h, prevstate_c = a
    current_word_id = np.argmax(probs)
    current_word = vocab.idx_to_word[current_word_id]
    print current_word
    

def GetText(s, seq_len):
  ws = [vocab.idx_to_word[s[0, i]] for i in range(seq_len)]
  return ' '.join(ws)

def Eval(expdir):
  print 'loading model'
  saver.restore(session, os.path.join(expdir, 'model.bin'))

  v = vocab

  total_word_count = 0
  total_log_prob = 0
  for pos in xrange(dataset.GetNumBatches()):
    print pos
    s, seq_len, usernames = dataset.GetNextBatch()

    feed_dict = {
        model.x: s[:, :-1],
        model.y: s[:, 1:],
        model.seq_len: seq_len,
      # model.username: usernames
    }

    a = session.run([model.cost], feed_dict=feed_dict)[0]
      

    m = model
    sess = session

    total_word_count += sum(seq_len)
    total_log_prob += float(a * sum(seq_len))

    print np.exp(total_log_prob / total_word_count)

if args.mode == 'train':
  Train(args.expdir)

if args.mode == 'eval':
  Eval(args.expdir)

if args.mode == 'debug':
  Greedy(args.expdir)
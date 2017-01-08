import bunch
import json
import logging
import os
import time
import shutil

import tensorflow as tf

from vocab import Vocab
from batcher import ReadData, Dataset
from model import HyperModel, MikolovModel

tf.app.flags.DEFINE_string("expdir", "",
                           "Where to save all the files")
tf.app.flags.DEFINE_string("params", "default_params.json",
                           "Where to get the hyperprameters from")

# Flags for defining the tf.train.ClusterSpec
tf.app.flags.DEFINE_string("ps_hosts", "",
                           "Comma-separated list of hostname:port pairs")
tf.app.flags.DEFINE_string("worker_hosts", "",
                           "Comma-separated list of hostname:port pairs")

# Flags for defining the tf.train.Server
tf.app.flags.DEFINE_string("job_name", "", "One of 'ps', 'worker'")
tf.app.flags.DEFINE_integer("task_index", 0, "Index of task within the job")
tf.app.flags.DEFINE_integer("worker_threads", 8, "Threads per worker")

FLAGS = tf.app.flags.FLAGS

def main(_):
  ps_hosts = FLAGS.ps_hosts.split(",")
  worker_hosts = FLAGS.worker_hosts.split(",")

  # Create a cluster from the parameter server and worker hosts.
  cluster = tf.train.ClusterSpec({"ps": ps_hosts, "worker": worker_hosts})

  # Create and start a server for the local task.
  server = tf.train.Server(cluster,
                           job_name=FLAGS.job_name,
                           task_index=FLAGS.task_index)


  with open(FLAGS.params, 'r') as f:
    params = bunch.Bunch(json.load(f))
  shutil.copy(FLAGS.params, os.path.join(FLAGS.expdir, 'params.json'))

  if FLAGS.job_name == "ps":

    if FLAGS.task_index == 0:
      usernames, texts = ReadData('/s0/ajaech/clean.tsv.bz', mode='train')
      dataset = Dataset(max_len=params.max_len + 1, batch_size=params.batch_size, preshuffle=True)
      dataset.AddDataSource(usernames, texts)
    
      vocab = Vocab.MakeFromData(texts, min_count=20)
      username_vocab = Vocab.MakeFromData([[u] for u in usernames],
                                        min_count=50)  

      vocab.Save(os.path.join(FLAGS.expdir, 'word_vocab.pickle'))
      username_vocab.Save(os.path.join(FLAGS.expdir, 'username_vocab.pickle'))

      os.mkdir(os.path.join(FLAGS.expdir, 'READY'))

    server.join()
   
  elif FLAGS.job_name == "worker":

    usernames, texts = ReadData(
      '/n/falcon/s0/ajaech/clean.tsv.bz',
      mode='train',
      worker=FLAGS.task_index,
      num_workers=len(worker_hosts)
    )
    dataset = Dataset(max_len=params.max_len + 1, preshuffle=True,
                      batch_size=params.batch_size)
    dataset.AddDataSource(usernames, texts)

    while not os.path.exists(os.path.join(FLAGS.expdir, 'READY')):
      print('waiting for READY file')
      time.sleep(1)  # wait for ready file
    print 'READY'

    vocab = Vocab.Load(os.path.join(FLAGS.expdir, 'word_vocab.pickle'))
    username_vocab = Vocab.Load(os.path.join(FLAGS.expdir, 
                                             'username_vocab.pickle'))
    print 'preparing dataset'
    dataset.Prepare(vocab, username_vocab)

    print 'building model'
    # Assigns ops to the local worker by default.
    with tf.device(tf.train.replica_device_setter(
        worker_device="/job:worker/task:%d" % FLAGS.task_index,
        cluster=cluster)):

      global_step = tf.Variable(0)
      models = {'hyper': HyperModel, 'mikolov': MikolovModel}
      model = models[params.model](params, len(vocab), len(username_vocab),
                                   use_nce_loss=True)

      tvars = tf.trainable_variables()
      optimizer = tf.train.AdamOptimizer(0.0001)
      grads, _ = tf.clip_by_global_norm(tf.gradients(model.cost, tvars), 5.0)
      train_op = optimizer.apply_gradients(zip(grads, tvars),
                                           global_step=global_step)

      saver = tf.train.Saver()

      init_op = tf.initialize_all_variables()

    logging.basicConfig(
      filename=os.path.join(FLAGS.expdir, 'worker{0}.log'.format(FLAGS.task_index)),
      level=logging.INFO)

    # Create a "supervisor", which oversees the training process.
    print 'creating supervisor'
    sv = tf.train.Supervisor(is_chief=(FLAGS.task_index == 0),
                             logdir=FLAGS.expdir,
                             init_op=init_op,
                             summary_op=None,
                             saver=saver,
                             global_step=global_step,
                             save_model_secs=900)

    # The supervisor takes care of session initialization, restoring from
    # a checkpoint, and closing when done or an error occurs.
    config = tf.ConfigProto(inter_op_parallelism_threads=FLAGS.worker_threads,
                            intra_op_parallelism_threads=FLAGS.worker_threads)
    with sv.managed_session(server.target, config=config) as sess:
      # Loop until the supervisor shuts down or 1000000 steps have completed.
      step = 0
      start_time = time.time()
      while not sv.should_stop() and step < 1000000:
        # Run a training step asynchronously.
        # See `tf.train.SyncReplicasOptimizer` for additional details on how to
        # perform *synchronous* training.

        s, seq_len, usernames = dataset.GetNextBatch()
        
        feed_dict = {
          model.x: s[:, :-1],
          model.y: s[:, 1:],
          model.seq_len: seq_len,
          model.username: usernames,
          model.dropout_keep_prob: params.dropout_keep_prob
        }        
        
        cost, _, step = sess.run([model.cost, train_op, global_step], 
                                 feed_dict=feed_dict)

        if step % 10 == 0:
          logging.info({'iter': step, 'cost': float(cost), 
                        'time': time.time() - start_time})
          print step, cost

    # Ask for all the services to stop.
    sv.stop()

if __name__ == "__main__":
  tf.app.run()
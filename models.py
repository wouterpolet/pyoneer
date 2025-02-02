
import os

from collections.abc import Iterable

from omegaconf import OmegaConf

import tensorflow as tf

from tensorflow.keras.layers import Conv2D, Dense, Flatten, MaxPooling2D, \
    GlobalAveragePooling2D, BatchNormalization, Dropout
    
import func, improc


#%% Model architectures

def get_simple_model():
    
    inp = tf.keras.Input(shape=(3, 32, 32))
    
    x = Flatten()(inp)
    x = Dense(512, activation=tf.nn.relu)(x)
    x = Dense(512, activation=tf.nn.relu)(x)
    output = Dense(10, activation='softmax')(x)

    model = tf.keras.Model(inputs = [inp], outputs = [output])
    
    return model

def get_model_conv_small(activation, dropout):
    
    # CPU only allows 'channels_last' order
    data_format = 'channels_last' if not tf.test.is_gpu_available() else 'channels_first'
    bn_axis = 3 if data_format == 'channels_last' else 1    # channel axis for batch normalization
    
    # parameters for convolutional layers:
    conv_layer_params = {
        'kernel_initializer': 'he_uniform',
        'padding': 'same',
        'data_format': data_format
    }

    if activation == 'LeakyReLU':
        conv_layer_params['activation'] = tf.keras.layers.LeakyReLU(alpha = 0.1)
    else:
        conv_layer_params['activation'] = tf.keras.layers.ReLU()
        
    inp = tf.keras.Input(shape = (3, 32, 32))

    if data_format == 'channels_last':
        x = tf.keras.layers.Lambda(lambda x: tf.transpose(x, [0, 2, 3, 1]))(inp)    # transform to 'channel_last'
    else:
        x = inp
    
    for i in range(3):
        x = Conv2D(96, (3, 3), **conv_layer_params)(x)
        x = BatchNormalization(axis = bn_axis)(x)
    
    x = MaxPooling2D(pool_size = (2, 2), data_format = data_format)(x)    
    x = Dropout(dropout)(x)

    for i in range(3):
        x = Conv2D(192, (3, 3), **conv_layer_params)(x)
        x = BatchNormalization(axis = bn_axis)(x)

    x = MaxPooling2D(pool_size = (2, 2), data_format = data_format)(x)    
    x = Dropout(dropout)(x)

    x = Conv2D(192, (3, 3), **conv_layer_params)(x)
    x = BatchNormalization(axis = bn_axis)(x)
    for i in range(2):
        x = Conv2D(192, (1, 1), **conv_layer_params)(x)
        x = BatchNormalization(axis = bn_axis)(x)
    
    x = GlobalAveragePooling2D(data_format = data_format)(x) 
    
    output = Dense(10, activation='softmax')(x)
    
    model = tf.keras.Model(inputs = [inp], outputs = [output])
    
    return model


#%% Model classes with custom training

class SemiSupervisedConsistencyModel(tf.keras.Model):
    
    def __init__(self, p, *args, **kwargs):
        super(SemiSupervisedConsistencyModel, self).__init__(*args, **kwargs)
        
        # if p.transform_output is specified, fill in missing values with values from p.transform
        if p.transform_output:
            p.transform_output = OmegaConf.merge(p.transform,
                                                 {} if p.transform_output == True else p.transform_output)
        
        self.p = p
        
    
    def compile(self, optimizer, loss, metrics = [], run_eagerly = False):
        """
        Compile the model.

        Parameters
        ----------
        p : parameters (an OmegaConf object)
        optimizer : a keras optimizer
            A keras optimizer. See tf.keras.optimizers. 
        loss : TF function
            A loss function to be used for supervised and unsupervised terms.
        metrics : a list of keras metrics, optional
            Metrics to be computed for labeled and unlabeled examples.
            See self.update_metrics to see how they are handled.
        run_eagerly : bool, optional
            If True, this Model's logic will not be wrapped in a tf.function;
            one thus can debug it more easily (e.g. print inside train_step).
            The default is False.

        Returns
        -------
        None.

        """
        super(SemiSupervisedConsistencyModel, self).compile()
        
        self.optimizer = optimizer
        self.loss = loss
        self.loss_trackers = [tf.keras.metrics.Mean(name = 'loss'),
                              tf.keras.metrics.Mean(name = 'loss_sup'),
                              tf.keras.metrics.Mean(name = 'loss_usup')]
        self.extra_metrics = metrics
        
        self._run_eagerly = run_eagerly
    
        
    def compute_loss(self, data):
        """
        Compute total loss:
            supervised + unsupervised consistency loss.

        Parameters
        ----------
        data : tuple
            The output of the generator.

        Returns
        -------
        loss_value : scalar
            Total loss.
        loss_sup : scalar
            Supervised loss.
        loss_usup : scalar
            Unsupervised loss.
        pair_sup : a tuple of tensors
            Ground truth labels and predictions on labeled examples.
        pair_usup : a tuple of tensors
            Predictions on two differently transformed labeled and unlabeled examples.
        """
        
        inputs, y, labeled = data
        
        x = inputs[0]
        transform_parameters = inputs[1:]
        
        # number of labeled and labeled+unlabeled images
        n_labeled = tf.cast(tf.math.count_nonzero(labeled), tf.int32)
        n = tf.shape(x)[0]
        
        # get a transform function
        transform = getattr(func, 'get_batch_transform_' +  self.p.transform.apply_func) \
                (*transform_parameters, **self.p.transform.params_apply)
                
        t_x = transform(x)                  # transform input images
        x = tf.concat((x, t_x), axis=0)   # form a batch to feed to the network

        # if network outputs and labels also need to be transformed (as in the segmentation case):
        if self.p.transform_output:        
            transform_output = getattr(func, 'get_batch_transform_' +  self.p.transform_output.apply_func) \
                (*transform_parameters, **self.p.transform_output.params_apply)
            t_y = transform_output(y)           # transform GT labels
            y = tf.concat((y, t_y), axis=0)   # form a batch corresponding to x
        else:
            y = tf.concat((y, y), axis=0)
        
        # save original and transformed inputs when in the debugging mode:
        if self.p.debug and self.run_eagerly:            
            improc.plot_batch_sample(self.p, x.numpy(), y.numpy(),
                                     os.path.join(self.p.results_path, self.p.exp_name, 'debug/model_input.png'))
            improc.plot_batch_sample(self.p, x[n:, ...].numpy(), y[n:, ...].numpy(),
                                     os.path.join(self.p.results_path, self.p.exp_name, 'debug/model_transformed_input.png'))

        # compute predictions on all examples
        pred = self(x)

        # separate differently transformed images
        pred1, pred2 = pred[:n, ...], pred[n:, ...]
        
        # separate labeled images from the rest
        yl = tf.concat((y[:n_labeled, ...], y[n:(n+n_labeled), ...]), axis=0)
        predl = tf.concat((pred1[:n_labeled, ...], pred2[:n_labeled, ...]), axis=0)

        # supervised loss
        loss_sup = tf.cond(tf.math.equal(tf.size(yl), 0),
                           lambda: 0.0,
                           lambda: self.loss(yl, predl))     

        if self.p.transform_output:
            # transform the first half of the predictions to align it with the second half:
            # (for unsupervised loss only!)
            t_pred1 = transform_output(pred1)
            # unsupervised loss made symmetric (e.g. KL divergence is not symmetric)
            loss_usup = (self.loss(t_pred1, pred2) + self.loss(pred2, t_pred1)) / 2
        else:
            # unsupervised loss made symmetric (e.g. KL divergence is not symmetric)
            loss_usup = (self.loss(pred1, pred2) + self.loss(pred2, pred1)) / 2
        
        # total loss: supervised + weight * unsupervised consistency
        loss_value = loss_sup + self.p.alpha * loss_usup
        
        return loss_value, loss_sup, loss_usup, (yl, predl), (pred1, pred2)
    
    def update_metrics(self, data, loss_values, pair_sup, pair_usup):
        """
        Updates loss trackers and metrics so that they return the current moving averages.

        """

        # update all the loss trackers with current batch loss values
        for loss_tracker, loss_value in zip(self.loss_trackers, loss_values):
            loss_tracker.update_state(loss_value)

        # obtain prediction - target pairs       
        yl, predl = pair_sup
        pred1, pred2 = pair_usup
        
        # for every metric type
        # sup:      metrics on the labeled subset measuring GT vs clean prediction fidelity
        # usup:     metrics on the entire batch measuring consistency
        for metric_type, y_true, y_pred in zip(['sup', 'usup'],
                                             [yl, pred1],
                                             [predl, pred2]):
            
            for metric in self.extra_metrics:
                
                # if metric name contains the type name
                if metric_type in metric.name.split('_'):
                    metric.update_state(y_true, y_pred)
            
        return {m.name: m.result() for m in self.metrics}
    
    def train_step(self, data):
        """
        This method is called by model.fit() for every batch.
        It should compute gradients, update model parameters and metrics.

        Parameters
        ----------
        data : tuple
            Batch received from the generator.

        Returns
        -------
        metric_values : dictionary
            Current values of all metrics (including loss terms).

        """

        # compute gradient wrt parameters
        with tf.GradientTape() as tape:
            loss_value, loss_sup, loss_usup, pair_sup, pair_usup = self.compute_loss(data)

        grads = tape.gradient(loss_value, self.trainable_variables)
        
        self.optimizer.apply_gradients(zip(grads, self.trainable_variables))        
        
        metric_values = self.update_metrics(data, [loss_value, loss_sup, loss_usup], pair_sup, pair_usup)

        return metric_values
        
    def test_step(self, data):
        """
        This method is called by model.fit() during the validation step
        and by model.evaluate().

        """

        loss_value, loss_sup, loss_usup, pair_sup, pair_usup = self.compute_loss(data)
        
        metric_values = self.update_metrics(data, [loss_value, loss_sup, loss_usup], pair_sup, pair_usup)
        
        return metric_values

    @property
    def metrics(self):
        # We list our `Metric` objects here so that `reset_states()` can be
        # called automatically at the start of each epoch
        # or at the start of `evaluate()`.
        # If you don't implement this property, you have to call
        # `reset_states()` yourself at the time of your choosing.
        return self.loss_trackers + self.extra_metrics
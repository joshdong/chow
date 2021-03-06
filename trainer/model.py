"""Defines data generator, augmentations, and the Keras model for training."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
from sklearn.metrics import accuracy_score, f1_score, hamming_loss, precision_score, recall_score, multilabel_confusion_matrix
import numpy as np

import imgaug as ia
import imgaug.augmenters as iaa
import imageio

import time

class DataGenerator(tf.keras.utils.Sequence):
    """
    Data generator that loads files from disk for model training.
    """
    def __init__(self, list_IDs, labels, image_dir, batch_size=32, dim=(224, 224), n_channels=3,
                 n_ingrs=3619, n_classes=1048, shuffle=True):
        'Initialization'
        self.dim = dim
        self.batch_size = batch_size
        self.labels = labels
        self.list_IDs = list_IDs
        self.n_channels = n_channels
        self.n_ingrs = n_ingrs
        self.n_classes = n_classes
        self.shuffle = shuffle
        self.on_epoch_end()
        self.image_dir = image_dir

    def __len__(self):
        'Denotes the number of batches per epoch'
        return int(np.floor(len(self.list_IDs) / self.batch_size))

    def __getitem__(self, index):
        'Generate one batch of data'
        # Generate indexes of the batch
        indexes = self.indexes[index*self.batch_size:(index+1)*self.batch_size]

        # Find list of IDs
        list_IDs_temp = [self.list_IDs[k] for k in indexes]

        # Generate data
        X, y = self.__data_generation(list_IDs_temp)

        return X, y

    def on_epoch_end(self):
        'Updates indexes after each epoch'
        self.indexes = np.arange(len(self.list_IDs))
        if self.shuffle == True:
            np.random.shuffle(self.indexes)

    def __augment_img(self, img):
        'Scale, pad, and augment the image.'
        w = img.shape[0]
        h = img.shape[1]
        scale = min(self.dim[0] / w, self.dim[1] / h) # choose whichever scales down the most
        
        seq = iaa.Sequential([
            iaa.Resize(float(scale)),
            iaa.CenterPadToFixedSize(width=self.dim[0], height=self.dim[1]),
            iaa.Fliplr(0.5),
            iaa.Affine(rotate=[0,90,180,270]),
        ])
        img = seq(image=img) / 255 # 0 to 1 range
        return img

    def __data_generation(self, list_IDs_temp):
        'Generates data containing batch_size samples' # X : (n_samples, *dim, n_channels)
        # Initialize batch
        X = np.empty((self.batch_size, *self.dim, self.n_channels))
        y = np.zeros((self.batch_size, self.n_ingrs)) # TODO: make dtype=object for 3 labels, but might lose their types
        
        # TESTING: time I/O
        fetch_time = 0
        aug_time = 0
        encode_time = 0
        
        # Fetch and store batch data
        for i, ID in enumerate(list_IDs_temp):
            # Augment image
            start = time.time()
            path = self.image_dir + '/'.join(ID[i] for i in range(4)) + '/' + ID
            img = imageio.imread(path)
            fetch_time += time.time() - start
            
            start = time.time()
            X[i,] = self.__augment_img(img)
            aug_time += time.time() - start
            
            # One hot encode label
            start = time.time()
            ingr_IDs = self.labels[ID][0] # col 0 is ingr ids, col 1 is class id, col 2 is num steps
            for j in ingr_IDs:
                y[i][j] = 1
            encode_time += time.time() - start
            # TODO: when returning more than a single class potential ValueError: 
            # failed to convert np array to tensor (unsupported object type tuple)
        
        total_time = fetch_time + aug_time + encode_time
        print("Seconds needed to fetch, aug, encode one batch of size {}: {:.2f}, {:.2f}, {:.2f}, TOTAL {:.2f}".format(self.batch_size, fetch_time, aug_time, encode_time, total_time))
        return X, y

def create_init_model(init_params):
    """
    Returns a neural net with a frozen pretrained model and trainable dense output layer.

    Followed https://www.tensorflow.org/guide/keras/transfer_learning#build_a_model to 
    keep batch norm layers of the pretrained model frozen (i.e. in inference mode) even 
    when the pretrained model layers are unfrozen subsequently.

    The model preprocesses input to scale pixel values between -1 and 1. 
    """
    # Unpack parameters
    input_shape, lr, num_ingrs, model_name = init_params['input_shape'], init_params['lr'], init_params['num_ingrs'], init_params['model_name']

    # Setup model
    if model_name == 'resnet':
        base = tf.keras.applications.ResNet50
        preprocess = tf.keras.applications.resnet.preprocess_input # 0 centered
    elif model_name == 'inception':
        base = tf.keras.applications.InceptionV3
        preprocess = tf.keras.applications.inception_v3.preprocess_input # -1 to 1
    elif model_name == 'densenet':
        base = tf.keras.applications.DenseNet121
        preprocess = tf.keras.applications.densenet.preprocess_input # 0 to 1
    else:
        print("Invalid model name provided in the params dictionary. \
            Options are 'resnet', 'inception', 'densenet'.")
        return
    
    base_model = base(input_shape=input_shape, include_top=False)
    base_model.trainable = False

    inputs = tf.keras.Input(shape=input_shape)
    x = tf.cast(inputs, tf.float32)
    x = preprocess(x) # scales pixel values 
    x = base_model(x, training=False) # keeps batchnorm layers frozen
    x = tf.keras.layers.GlobalAveragePooling2D()(x)
    outputs = tf.keras.layers.Dense(num_ingrs, activation='softmax')(x)
    model = tf.keras.Model(inputs, outputs)

    # Compile model
    optimizer = tf.keras.optimizers.RMSprop(lr=lr)
    metrics = [f1_ml, prec_ml, recall_ml]

    model.compile(
        optimizer=optimizer,
        loss=tf.keras.losses.CategoricalCrossentropy(), # expects model output that's [0, 1]
        metrics=metrics)
    return model

def create_tl_model(tl_params):
    """
    Returns a neural net with its last few pretrained layers trainable. Initialized with a 
    pretrained dense output layer loaded from the saved model path.
    """
    # Unpack parameters
    lr, num_unfreeze, init_path = tl_params['lr'], tl_params['num_unfreeze'], tl_params['init_path']

    # Load best stage 1 model weights
    nn = tf.keras.models.load_model(init_path, compile=False)
    init_num_trainable = len(nn.trainable_variables)
    print("\nStage 1 model summary (loaded in)")
    nn.summary()
    
    # Unfreeze last few layers
    base = nn.layers[3]
    base.trainable = True
    for layer in base.layers[:-num_unfreeze]:
        layer.trainable = False

    tl_num_trainable = len(nn.trainable_variables)
    print("\n\nStage 2 model summary (unfrozen)")
    nn.summary()
    print("\n\nNumber of trainable variables: {} to {}".format(init_num_trainable, tl_num_trainable))
    
    # Compile model    
    optimizer = tf.keras.optimizers.RMSprop(lr=lr)
    metrics = [f1_ml, prec_ml, recall_ml]

    nn.compile(optimizer=optimizer,
                loss=tf.keras.losses.CategoricalCrossentropy(), # expects model output that's [0, 1]
                metrics=metrics)
    
    return nn

# Functions that return multilabel performance metrics
def eval_as_np(fn, y_true, y_pred):
    return tf.numpy_function(fn, [y_true, tf.round(y_pred)], tf.double)

def f1_score_macro(y_true, y_pred):
    return f1_score(y_true, y_pred, average='macro')

def f1_ml(y_true, y_pred):
    return eval_as_np(f1_score_macro, y_true, y_pred)

def prec_score_macro(y_true, y_pred):
    return precision_score(y_true, y_pred, average='macro')

def prec_ml(y_true, y_pred):
    return eval_as_np(prec_score_macro, y_true, y_pred)

def recall_score_macro(y_true, y_pred):
    return recall_score(y_true, y_pred, average='macro')

def recall_ml(y_true, y_pred):
    return eval_as_np(recall_score_macro, y_true, y_pred)

def accuracy_ml(y_true, y_pred):
    return eval_as_np(accuracy_score, y_true, y_pred)

def hamming_ml(y_true, y_pred):
    return eval_as_np(hamming_loss, y_true, y_pred)

def max_prob(y_true, y_pred):
    return tf.math.reduce_max(y_pred)
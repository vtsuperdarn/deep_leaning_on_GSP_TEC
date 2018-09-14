'''
This file gets the predicted tec maps by first loading the saved model and then running on the test input.
'''
import matplotlib
matplotlib.use("Agg")

import sys
sys.path.append("../STResNet")
from st_resnet import STResNetShared, STResNetIndep
import tensorflow as tf
from params import Params as param
import pandas as pd
import numpy as np
import sqlite3
from tqdm import tqdm
import datetime as dt
import os
import shutil
import numpy
import time
from omn_utils import OmnData
from batch_utils import BatchDateUtils, TECUtils

# Extract hyperparameter values from saved_model_path
param_values = param.saved_model_path.split("/")[-1].split("_")
param.batch_size = [int(x.replace("batch", "")) for x in param_values if x.startswith("batch")][0]
param.num_epochs = [int(x.replace("epoch", "")) for x in param_values if x.startswith("epoch")][0]
param.num_of_residual_units = [int(x.replace("resnet", "")) for x in param_values if x.startswith("resnet")][0]
param.resnet_out_filters = [int(x.replace("nresfltr", "")) for x in param_values if x.startswith("nresfltr")][0]
param.num_of_filters = [int(x.replace("nfltr", "")) for x in param_values if x.startswith("nfltr")][0]
param.output_freq = [int(x.replace("of", "")) for x in param_values if x.startswith("of")][0]
param.num_of_output_tec_maps = [int(x.replace("otec", "")) for x in param_values if x.startswith("otec")][0]
param.closeness_freq = [int(x.replace("cf", "")) for x in param_values if x.startswith("cf")][0]
param.closeness_sequence_length = [int(x.replace("csl", "")) for x in param_values if x.startswith("csl")][0]
param.period_freq = [int(x.replace("pf", "")) for x in param_values if x.startswith("pf")][0]
param.period_sequence_length = [int(x.replace("psl", "")) for x in param_values if x.startswith("psl")][0]
param.trend_freq = [int(x.replace("tf", "")) for x in param_values if x.startswith("tf")][0]
param.trend_sequence_length = [int(x.replace("tsl", "")) for x in param_values if x.startswith("tsl")][0]
param.gru_size = [int(x.replace("gs", "")) for x in param_values if x.startswith("gs")][0]
ks = [x.replace("ks", "") for x in param_values if x.startswith("ks")][0]
param.kernel_size = (int(ks[0]), int(ks[1]))
exo = [x.replace("exo", "") for x in param_values if x.startswith("exo")][0]
nrm = [x.replace("nrm", "") for x in param_values if x.startswith("nrm")][0]
if exo == "T":
    param.add_exogenous = True
else:
    param.add_exogenous = False 
if nrm == "T":
    param.imf_normalize = True
else:
    param.imf_normalize = False 

#closeness is sampled 12 times every 5 mins, lookback = (12*5min = 1 hour)
#freq 1 is 5mins
#size corresponds to the sample size
closeness_size = param.closeness_sequence_length

#period is sampled 24 times every 1 hour (every 12th index), lookback = (24*12*5min = 1440min = 1day)
period_size = param.period_sequence_length

#trend is sampled 24 times every 3 hours (every 36th index), lookback = (8*36*5min = 1440min = 1day)
trend_size = param.trend_sequence_length

# We need OMNI data for testing
# setting appropriate vars 
omn_train=False
start_date_omni =  param.start_date - dt.timedelta(days=param.load_window)
end_date_omni =  param.end_date + dt.timedelta(days=param.load_window)

# Copy the trained model to current folder
if not os.path.exists(param.saved_model_path):
    print("Please copy the model to ./model_results/")
    return

path = param.saved_model_path+"_values"

#getting the omni object
omnObj = OmnData(start_date_omni, end_date_omni, param.omn_dbdir, param.omn_db_name, param.omn_table_name, omn_train, param.imf_normalize, path)

# get all corresponding dates for batches
batchObj = BatchDateUtils(param.start_date, param.end_date, param.batch_size, param.tec_resolution, param.data_point_freq,\
                         param.closeness_freq, closeness_size, param.period_freq, period_size,\
                         param.trend_freq, trend_size, param.num_of_output_tec_maps, param.output_freq,\
                         param.closeness_channel, param.period_channel, param.trend_channel)
                      
#getting all the datetime from which prediction has to be made                                                  
date_arr_test = np.array( list(batchObj.batch_dict.keys()) )

# Bulk load TEC data
tecObj = TECUtils(param.start_date, param.end_date, param.file_dir, param.tec_resolution, param.load_window,\
                 param.closeness_channel, param.period_channel, param.trend_channel)
                 
weight_matrix = np.load(param.loss_weight_matrix)
#converting by repeating the weight_matrix into a desired shape of (B, O, H, W)
weight_matrix_expanded = np.expand_dims(weight_matrix, 0)
weight_matrix_tiled = np.tile(weight_matrix_expanded, [param.batch_size*param.num_of_output_tec_maps, 1, 1])
loss_weight_matrix = np.reshape(weight_matrix_tiled, [param.batch_size, param.num_of_output_tec_maps, param.map_height, param.map_width])

#converting the dimension from (B, O, H, W) -> (B, H, W, O)
loss_weight_matrix = np.transpose(loss_weight_matrix, [0, 2, 3, 1])

#creating directory inside the model_path_values folder for those datetime variables for which prediction is made
path_pred = path+'/'+"predicted_tec"
if not os.path.exists(path_pred):
    os.makedirs(path_pred)

# Parameters for tensor flow
if(param.independent_channels == True): 
    g = STResNetIndep()
    print ("Computation graph for ST-ResNet with independent channels loaded\n")

else:
    g = STResNetShared()
    print ("Computation graph for ST-ResNet with shared channels loaded\n")

with tf.Session(graph=g.graph) as sess:
    #loading the trained model whose path is given in the params file
    g.saver.restore(sess, param.saved_model_path+param.saved_model)
    
    b = 1
    loss_values = []
    for te_ind, current_datetime in tqdm(enumerate(date_arr_test)):
        #print("Testing date-->" + current_datetime.strftime("%Y%m%d-%H%M"))

        #get the batch of data points
        curr_batch_time_dict = batchObj.batch_dict[current_datetime]
                
        data_close, data_period, data_trend, data_out = tecObj.create_batch(curr_batch_time_dict)
        
        #if we need to use the exogenous module
        if (param.add_exogenous == True):
            imf_batch = omnObj.get_omn_batch(current_datetime, param.batch_size, param.trend_freq, trend_size )
            
            if(param.closeness_channel == True and param.period_channel == True and param.trend_channel == True):
                data_close, data_period, data_trend, data_out = tecObj.create_batch(curr_batch_time_dict)
                loss_v, pred, truth, closeness, period, trend = sess.run([g.loss, g.x_res, g.output_tec, g.exo_close, g.exo_period, g.exo_trend],
                                                                 feed_dict={g.c_tec: data_close,
                                                                            g.p_tec: data_period,
                                                                            g.t_tec: data_trend,
                                                                            g.output_tec: data_out,
                                                                            g.exogenous: imf_batch,
                                                                            g.loss_weight_matrix: loss_weight_matrix})
            elif(param.closeness_channel == True and param.period_channel == True and param.trend_channel == False):
                #here the data_trend will be empty
                data_close, data_period, data_trend, data_out = tecObj.create_batch(curr_batch_time_dict)
                loss_v, pred, truth, closeness, period = sess.run([g.loss, g.x_res, g.output_tec, g.exo_close, g.exo_period],
                                                                 feed_dict={g.c_tec: data_close,
                                                                            g.p_tec: data_period,
                                                                            g.output_tec: data_out,
                                                                            g.exogenous: imf_batch,
                                                                            g.loss_weight_matrix: loss_weight_matrix})
            elif(param.closeness_channel == True and param.period_channel == False and param.trend_channel == True):          
                #here the data_period will be empty
                data_close, data_period, data_trend, data_out = tecObj.create_batch(curr_batch_time_dict)
                loss_v, pred, truth, closeness, trend = sess.run([g.loss, g.x_res, g.output_tec, g.exo_close, g.exo_trend],
                                                                 feed_dict={g.c_tec: data_close,
                                                                            g.t_tec: data_trend,
                                                                            g.output_tec: data_out,
                                                                            g.exogenous: imf_batch,
                                                                            g.loss_weight_matrix: loss_weight_matrix})
            elif(param.closeness_channel == True and param.period_channel == False and param.trend_channel == False):
                #here the data_period, data_trend will be empty
                data_close, data_period, data_trend, data_out = tecObj.create_batch(curr_batch_time_dict)
                loss_v, pred, truth, closeness = sess.run([g.loss, g.x_res, g.output_tec, g.exo_close],
                                                                 feed_dict={g.c_tec: data_close,
                                                                            g.output_tec: data_out,
                                                                            g.exogenous: imf_batch,
                                                                            g.loss_weight_matrix: loss_weight_matrix})
        #if we dont want to use the exogenous module                                                                    
        else:
            if(param.closeness_channel == True and param.period_channel == True and param.trend_channel == True):
                data_close, data_period, data_trend, data_out = tecObj.create_batch(curr_batch_time_dict)
                loss_v, pred, truth, closeness, period, trend = sess.run([g.loss, g.x_res, g.output_tec, g.closeness_output, g.period_output, g.trend_output],
                                                                 feed_dict={g.c_tec: data_close,
                                                                            g.p_tec: data_period,
                                                                            g.t_tec: data_trend,
                                                                            g.output_tec: data_out,
                                                                            g.loss_weight_matrix: loss_weight_matrix})
            elif(param.closeness_channel == True and param.period_channel == True and param.trend_channel == False):
                #here the data_trend will be empty
                data_close, data_period, data_trend, data_out = tecObj.create_batch(curr_batch_time_dict)
                loss_v, pred, truth, closeness, period = sess.run([g.loss, g.x_res, g.output_tec, g.closeness_output, g.period_output],
                                                                 feed_dict={g.c_tec: data_close,
                                                                            g.p_tec: data_period,
                                                                            g.output_tec: data_out,
                                                                            g.loss_weight_matrix: loss_weight_matrix})  
            elif(param.closeness_channel == True and param.period_channel == False and param.trend_channel == True):
                #here the data_period will be empty
                data_close, data_period, data_trend, data_out = tecObj.create_batch(curr_batch_time_dict)
                loss_v, pred, truth, closeness, trend = sess.run([g.loss, g.x_res, g.output_tec, g.closeness_output, g.trend_output],
                                                                 feed_dict={g.c_tec: data_close,
                                                                            g.t_tec: data_trend,
                                                                            g.output_tec: data_out,
                                                                            g.loss_weight_matrix: loss_weight_matrix}) 
            elif(param.closeness_channel == True and param.period_channel == False and param.trend_channel == False):
                #here the data_period,data_trend will be empty
                data_close, data_period, data_trend, data_out = tecObj.create_batch(curr_batch_time_dict)
                loss_v, pred, truth, closeness = sess.run([g.loss, g.x_res, g.output_tec, g.closeness_output],
                                                                 feed_dict={g.c_tec: data_close,
                                                                            g.output_tec: data_out,
                                                                            g.loss_weight_matrix: loss_weight_matrix})
        loss_values.append(loss_v)    
        print("val_loss: {:.3f}".format(loss_v))        
        
        #saving the predictions into seperate directories that are already created
        j = 0
        for dtm in curr_batch_time_dict.keys():
            tec_pred = dtm.strftime("%Y%m%d.%H%M") + "_pred.npy"
            tec_true = dtm.strftime("%Y%m%d.%H%M") + "_true.npy"
            tec_close = dtm.strftime("%Y%m%d.%H%M") + "_close.npy"
            tec_period = dtm.strftime("%Y%m%d.%H%M") + "_period.npy"
            tec_trend = dtm.strftime("%Y%m%d.%H%M") + "_trend.npy"
            np.save(path_pred+tec_pred, pred[j])
            np.save(path_pred+tec_true, truth[j])
            if(param.closeness_channel == True):
                np.save(path_pred+tec_close, closeness[j])
            if(param.period_channel == True):
                np.save(path_pred+tec_period, period[j])
            if(param.trend_channel == True):    
                np.save(path_pred+tec_trend, trend[j])
            j += 1
            
        print ('Saving {} batch with {:.1f}'.format(b, loss_v.item()))
        b += 1
        
    loss_values = np.array(loss_values)
    print ('Saving loss values in the .npy file ...')    
    np.save(path_pred+'/prediction_loss.npy', loss_values)        
"""Calibre (Adaptive Ensemble) with hierarchical structure using MCMC and Penalized VI. """
import os
import time

from importlib import reload

import pickle as pk

import numpy as np

from sklearn.cluster import KMeans

import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow_probability import edward2 as ed

# sys.path.extend([os.getcwd()])

from calibre.model import gaussian_process as gp
from calibre.model import tailfree_process as tail_free
from calibre.model import adaptive_ensemble

from calibre.calibration import score

import calibre.util.visual as visual_util
import calibre.util.matrix as matrix_util
import calibre.util.data as data_util
import calibre.util.gp_flow as gpf_util
import calibre.util.calibration as calib_util

from calibre.util.data import sin_curve_1d, cos_curve_1d
from calibre.util.inference import make_value_setter
from calibre.util.gp_flow import DEFAULT_KERN_FUNC_DICT_GPY, DEFAULT_KERN_FUNC_DICT_GPFLOW

import matplotlib.pyplot as plt
import seaborn as sns

tfd = tfp.distributions

_FIT_BASE_MODELS = False
_FIT_MCMC_MODELS = False
_FIT_VI_MODELS = False

_EXAMPLE_DICTIONARY_SIMPLE = {
    "root": ["rbf",
             # "poly",
             # "period",
             "rquad"
             ],
    "rbf": [  # "rbf_1", "rbf_0.5",
        "rbf_0.2", "rbf_0.05", "rbf_0.01",
        # "rbf_auto"
    ],
    # "period": ["period0.5_0.15", "period1_0.15",
    #            "period1.5_0.15", "period_auto"],
    "rquad": [
        "rquad1_0.1", "rquad1_0.2", "rquad1_0.5",
        #    "rquad2_0.1", "rquad2_0.2", "rquad2_0.5", "rquad_auto"
    ],
    # "poly": ["poly_1", "poly_2", "poly_3"]
}

_EXAMPLE_DICTIONARY_FULL = {
    "root": ["rbf", "period", "rquad", "poly"],
    "rbf": ["rbf_1", "rbf_0.5", "rbf_0.2",
            "rbf_0.05", "rbf_0.01", "rbf_auto"],
    "period": ["period0.5_0.15", "period1_0.15",
               "period1.5_0.15", "period_auto"],
    "rquad": ["rquad1_0.1", "rquad1_0.2", "rquad1_0.5",
              "rquad2_0.1", "rquad2_0.2", "rquad2_0.5", "rquad_auto"],
    "poly": ["poly_1", "poly_2", "poly_3"]
}

for target_function in data_util.FUNC_LIST_2D:

    """""""""""""""""""""""""""""""""
    # 1. Generate data
    """""""""""""""""""""""""""""""""

    _SAVE_ADDR_PREFIX_ROOT = "./result/calibre_2d_tree"
    _SAVE_ADDR_PREFIX = "{}/{}".format(_SAVE_ADDR_PREFIX_ROOT,
                                       target_function.__name__)

    data_range = np.asarray([-1., 1.])

    data_all = data_util.generate_2d_data(target_function, size=1000,
                                          data_range=data_range,
                                          seed=100)
    data_valid = data_util.generate_2d_data(target_function, size=2500,
                                            data_range=data_range * 1.1,
                                            validation=True,
                                            visualize=False)

    train_perc = 0.3 if target_function.__name__ == "eggholder" else 0.1
    train_id, test_id = data_util.train_test_split_id(
        data_all.shape[0], train_perc=train_perc)

    X_all, y_all = data_all[:, :2], data_all[:, 2]
    X_valid, y_valid = data_valid[:, :2], data_valid[:, 2]

    X_train, y_train = X_all[train_id], y_all[train_id]
    X_test, y_test = X_all[test_id], y_all[test_id]

    X_test, y_test = X_test[:100], y_test[:100]

    # subsample from X_valid for calibration
    np.random.seed(100)
    calib_sample_id = np.where(
        (X_valid[:, 0] > data_range[0]) & (X_valid[:, 0] <= data_range[1]) &
        (X_valid[:, 1] > data_range[0]) & (X_valid[:, 1] <= data_range[1])
    )[0]
    calib_sample_id = np.random.choice(len(calib_sample_id), size=1000)

    if _FIT_BASE_MODELS:
        gpf_util.fit_base_gp_models(
            X_train, y_train,
            X_test, y_test,
            X_valid, y_valid,
            kern_func_dict=DEFAULT_KERN_FUNC_DICT_GPY,
            n_valid_sample=500,
            n_train_step=50000,
            save_addr_prefix="{}/base/".format(_SAVE_ADDR_PREFIX),
        )

    """""""""""""""""""""""""""""""""
    # 2. MCMC
    """""""""""""""""""""""""""""""""
    family_name = "hmc"
    family_name_full = "Hamilton MC"

    os.makedirs("{}/{}".format(_SAVE_ADDR_PREFIX, family_name),
                exist_ok=True)

    with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_test_pred.pkl'), 'rb') as file:
        base_test_pred = pk.load(file)

    with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_valid_pred.pkl'), 'rb') as file:
        base_valid_pred = pk.load(file)

    """2.1. sampler basic config"""
    y_test = y_test[:100].astype(np.float32)
    X_test = X_test[:100].astype(np.float32)

    N = X_test.shape[0]
    K = len(base_test_pred)

    family_tree_dict = {tail_free.ROOT_NODE_DEFAULT_NAME:
                            list(base_test_pred.keys())}  # use default dictionary for now.

    num_results = 5000
    num_burnin_steps = 5000

    if _FIT_MCMC_MODELS:
        # define mcmc computation graph
        mcmc_graph = tf.Graph()
        with mcmc_graph.as_default():
            # build likelihood by explicitly
            log_joint = ed.make_log_joint_fn(adaptive_ensemble.model_tailfree)

            # aggregate node-specific variable names
            cond_weight_temp_names = ['temp_{}'.format(model_name) for
                                      model_name in
                                      tail_free.get_parent_node_names(family_tree_dict)]
            node_weight_names = ['base_weight_{}'.format(model_name) for
                                 model_name in
                                 tail_free.get_nonroot_node_names(family_tree_dict)]
            node_specific_varnames = cond_weight_temp_names + node_weight_names


            def target_log_prob_fn(ls_weight, ls_resid, sigma,
                                   ensemble_resid,
                                   *node_specific_positional_args):
                """Unnormalized target density as a function of states."""
                # build kwargs for base model weight using positional args
                node_specific_kwargs = dict(zip(node_specific_varnames,
                                                node_specific_positional_args))

                return log_joint(X=X_test,
                                 base_pred=base_test_pred,
                                 family_tree=family_tree_dict,
                                 y=y_test.squeeze(),
                                 ls_weight=ls_weight,
                                 ls_resid=ls_resid,
                                 sigma=sigma,
                                 ensemble_resid=ensemble_resid,
                                 **node_specific_kwargs)


            # set up state container
            initial_state = [
                                # tf.random_normal([N, K], stddev=0.01, name='init_ensemble_weight'),
                                # tf.random_normal([N], stddev=0.01, name='init_f_ensemble'),
                                tf.constant(0.1, name='init_ls_weight'),
                                tf.constant(0.1, name='init_ls_resid'),
                                tf.constant(0.1, name='init_sigma'),
                                tf.random_normal([N], stddev=0.01,
                                                 name='init_ensemble_resid'),
                            ] + [
                                tf.random_normal([], stddev=0.01,
                                                 name='init_{}'.format(var_name)) for
                                var_name in cond_weight_temp_names
                            ] + [
                                tf.random_normal([N], stddev=0.01,
                                                 name='init_{}'.format(var_name)) for
                                var_name in node_weight_names
                            ]

            # set up HMC transition kernel
            step_size = tf.get_variable(
                name='step_size',
                initializer=1.,
                use_resource=True,  # For TFE compatibility.
                trainable=False)

            hmc = tfp.mcmc.HamiltonianMonteCarlo(
                target_log_prob_fn=target_log_prob_fn,
                num_leapfrog_steps=3,
                step_size=step_size,
                step_size_update_fn=tfp.mcmc.make_simple_step_size_update_policy())

            # set up main sampler
            state, kernel_results = tfp.mcmc.sample_chain(
                num_results=num_results,
                num_burnin_steps=num_burnin_steps,
                current_state=initial_state,
                kernel=hmc,
                parallel_iterations=1
            )

            ls_weight_sample, ls_resid_sample, sigma_sample, ensemble_resid_sample = state[:4]
            temp_sample = state[4:4 + len(cond_weight_temp_names)]
            weight_sample = state[4 + len(cond_weight_temp_names):]

            # set up init op
            init_op = tf.global_variables_initializer()

            mcmc_graph.finalize()

        """ 2.2. execute sampling"""
        with tf.Session(graph=mcmc_graph) as sess:
            init_op.run()
            [
                ls_weight_sample_val,
                ls_resid_sample_val,
                sigma_sample_val,
                temp_sample_val,
                resid_sample_val,
                weight_sample_val,
                is_accepted_,
            ] = sess.run(
                [
                    ls_weight_sample,
                    ls_resid_sample,
                    sigma_sample,
                    temp_sample,
                    ensemble_resid_sample,
                    weight_sample,
                    kernel_results.is_accepted,
                ])
            print('Acceptance Rate: {}'.format(np.mean(is_accepted_)))
            sess.close()

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/sigma_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(sigma_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/temp_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(temp_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/weight_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(weight_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_resid_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(resid_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_ls_resid_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(ls_weight_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_ls_weight_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(ls_resid_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)

        """ 2.3. prediction and visualization"""

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/sigma_sample.pkl'.format(family_name)), 'rb') as file:
            sigma_sample_val = pk.load(file)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/temp_sample.pkl'.format(family_name)), 'rb') as file:
            temp_sample_val = pk.load(file)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/weight_sample.pkl'.format(family_name)), 'rb') as file:
            weight_sample_val = pk.load(file)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_resid_sample.pkl'.format(family_name)), 'rb') as file:
            resid_sample_val = pk.load(file)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_ls_resid_sample.pkl'.format(family_name)), 'rb') as file:
            ls_weight_sample_val = pk.load(file)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_ls_weight_sample.pkl'.format(family_name)), 'rb') as file:
            ls_resid_sample_val = pk.load(file)

        """ 2.3.1. prediction """
        # compute GP prediction for weight GP and residual GP
        model_weight_valid_sample = []
        for model_weight_sample in weight_sample_val:
            model_weight_valid_sample.append(
                gp.sample_posterior_full(X_new=X_valid, X=X_test,
                                         f_sample=model_weight_sample.T,
                                         ls=np.exp(LOG_LS_WEIGHT_HMC),
                                         kernel_func=gp.rbf).T.astype(np.float32)
            )

        ensemble_resid_valid_sample = (
            gp.sample_posterior_full(X_new=X_valid, X=X_test,
                                     f_sample=resid_sample_val.T,
                                     ls=np.exp(LOG_LS_RESID_HMC),
                                     kernel_func=gp.rbf).T
        )

        # compute sample for posterior mean
        raw_weights_dict = dict(zip(tail_free.get_nonroot_node_names(family_tree_dict),
                                    model_weight_valid_sample))
        parent_temp_dict = dict(zip(tail_free.get_parent_node_names(family_tree_dict),
                                    temp_sample_val))

        (ensemble_sample_val, ensemble_mean_val,
         ensemble_weights_val, cond_weights_dict_val, ensemble_model_names) = (
            adaptive_ensemble.sample_posterior_tailfree(X=X_valid, base_pred_dict=base_valid_pred,
                                                        family_tree=family_tree_dict, weight_gp_dict=raw_weights_dict,
                                                        temp_dict=parent_temp_dict,
                                                        resid_gp_sample=ensemble_resid_valid_sample,
                                                        log_ls_weight=LOG_LS_WEIGHT_HMC))

        # compute covariance matrix among model weights
        model_weights_raw = np.asarray([raw_weights_dict[model_name]
                                        for model_name in ensemble_model_names])
        model_weights_raw = np.swapaxes(model_weights_raw, 0, -1)
        ensemble_weight_corr = matrix_util.corr_mat(model_weights_raw, axis=0)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_mean_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(ensemble_mean_val, file, protocol=pk.HIGHEST_PROTOCOL)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_dist_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(ensemble_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_node_weight_dict.pkl'.format(family_name)), 'wb') as file:
            pk.dump(cond_weights_dict_val, file, protocol=pk.HIGHEST_PROTOCOL)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_model_weights.pkl'.format(family_name)), 'wb') as file:
            pk.dump(ensemble_weights_val, file, protocol=pk.HIGHEST_PROTOCOL)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_model_weights_corr.pkl'.format(family_name)), 'wb') as file:
            pk.dump(ensemble_weight_corr, file, protocol=pk.HIGHEST_PROTOCOL)

        """ 2.3.3. visualize: ensemble posterior predictive mean """
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_mean_sample.pkl'.format(family_name)), 'rb') as file:
            ensemble_mean_val = pk.load(file)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_dist_sample.pkl'.format(family_name)), 'rb') as file:
            ensemble_sample_val = pk.load(file)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_model_weights.pkl'.format(family_name)), 'rb') as file:
            ensemble_weights_val = pk.load(file)

        posterior_mean_mu = np.nanmean(ensemble_mean_val, axis=0)
        posterior_mean_cov = np.nanvar(ensemble_mean_val, axis=0)

        posterior_mean_median = np.nanmedian(ensemble_mean_val, axis=0)
        posterior_mean_quantiles = [
            np.percentile(ensemble_mean_val,
                          [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
            for q in [68, 95, 99]
        ]

        visual_util.gpr_2d_visual(y_valid,
                                  pred_cov=posterior_mean_cov,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="True Surface",
                                  save_addr=os.path.join(
                                      _SAVE_ADDR_PREFIX, "ground_truth.png")
                                  )

        visual_util.gpr_2d_visual(posterior_mean_mu,
                                  pred_cov=posterior_mean_cov,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Mean, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_mean.png".format(family_name))
                                  )

        visual_util.gpr_2d_visual(posterior_mean_median,
                                  pred_cov=None,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Median, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_median.png".format(family_name))
                                  )

        """ 2.3.4. visualize: ensemble residual """

        posterior_resid_mu = np.nanmean(ensemble_resid_valid_sample, axis=0)
        posterior_resid_cov = np.nanvar(ensemble_resid_valid_sample, axis=0)

        posterior_resid_median = np.nanmedian(ensemble_resid_valid_sample, axis=0)
        posterior_resid_quantiles = [
            np.percentile(ensemble_resid_valid_sample,
                          [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
            for q in [68, 95, 99]
        ]

        visual_util.gpr_2d_visual(posterior_resid_mu,
                                  pred_cov=posterior_resid_cov,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Residual, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_resid.png".format(family_name))
                                  )

        visual_util.gpr_2d_visual(posterior_resid_median,
                                  pred_cov=None,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Residual, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_resid_median.png".format(family_name))
                                  )

        visual_util.gpr_2d_visual(np.sqrt(posterior_resid_cov),
                                  pred_cov=None,
                                  X_train=X_test, y_train=y_test * 0,
                                  X_test=X_valid, y_test=np.sqrt(posterior_resid_cov),
                                  title="Ensemble Posterior Residual Uncertainty, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_resid_cov.png".format(family_name))
                                  )

        """ 2.3.5. visualize: ensemble posterior full """

        posterior_dist_mu = np.nanmean(ensemble_sample_val, axis=0)
        posterior_dist_cov = np.nanvar(ensemble_sample_val, axis=0)

        posterior_dist_median = np.nanmedian(ensemble_sample_val, axis=0)
        posterior_dist_quantiles = [
            np.percentile(ensemble_sample_val,
                          [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
            for q in [68, 95, 99]
        ]

        visual_util.gpr_2d_visual(posterior_dist_mu,
                                  pred_cov=posterior_dist_cov,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Predictive, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_full.png".format(family_name))
                                  )

        visual_util.gpr_2d_visual(posterior_dist_median,
                                  pred_cov=posterior_dist_cov,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Predictive Median, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_full_median.png".format(family_name))
                                  )

        visual_util.gpr_2d_visual(np.sqrt(posterior_dist_cov),
                                  pred_cov=None,
                                  X_train=X_test, y_train=y_test * 0,
                                  X_test=X_valid, y_test=np.sqrt(posterior_dist_cov),
                                  title="Ensemble Posterior Residual Uncertainty, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_resid_cov.png".format(family_name))
                                  )

        """ 2.3.6. visualize: ensemble posterior reliability """
        y_calib = y_valid[calib_sample_id]
        y_sample_calib = ensemble_sample_val[:, calib_sample_id].T

        visual_util.prob_calibration_1d(
            y_calib, y_sample_calib,
            title="Ensemble, {}".format(family_name_full),
            save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                   "{}/ensemble_calibration_prob.png".format(family_name)))

        """ 2.3.7. visualize: base ensemble weight with uncertainty """
        visual_util.plot_ensemble_weight_mean_2d(X=X_valid, weight_sample=ensemble_weights_val,
                                                 model_names=ensemble_model_names,
                                                 save_addr_prefix=os.path.join(
                                                     _SAVE_ADDR_PREFIX, "{}/".format(family_name)))

        # # model family weights
        # ensemble_weights_family = np.stack(
        #     [cond_weights_dict_val[key] for key in family_tree_dict['root']], axis=-1)
        # visual_util.plot_ensemble_weight_mean_1d(X=X_valid,
        #                                          weight_sample=ensemble_weights_family,
        #                                          model_names=family_tree_dict['root'],
        #                                          save_addr_prefix=os.path.join(
        #                                              _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))
        # visual_util.plot_ensemble_weight_median_1d(X=X_valid,
        #                                            weight_sample=ensemble_weights_family,
        #                                            model_names=family_tree_dict['root'],
        #                                            save_addr_prefix=os.path.join(
        #                                                _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))
        #

    """""""""""""""""""""""""""""""""
    # 3. Variational Inference
    """""""""""""""""""""""""""""""""
    with open(os.path.join(_SAVE_ADDR_PREFIX,
                           'hmc/ensemble_ls_resid_sample.pkl'), 'rb') as file:
        ls_weight_sample_val = pk.load(file)
    with open(os.path.join(_SAVE_ADDR_PREFIX,
                           'hmc/ensemble_ls_weight_sample.pkl'), 'rb') as file:
        ls_resid_sample_val = pk.load(file)

    with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_test_pred.pkl'), 'rb') as file:
        base_test_pred = pk.load(file)

    with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_valid_pred.pkl'), 'rb') as file:
        base_valid_pred = pk.load(file)

    LOG_LS_WEIGHT_HMC = np.median(ls_weight_sample_val)
    LOG_LS_RESID_HMC = np.median(ls_resid_sample_val)

    """ 3.1. basic data/algorithm config"""

    X_induce = KMeans(n_clusters=10, random_state=100).fit(
        X_valid[calib_sample_id]).cluster_centers_.astype(np.float32)
    family_tree_dict = {tail_free.ROOT_NODE_DEFAULT_NAME:
                            list(base_test_pred.keys())}  # use default dictionary for now.

    n_inference_sample = 100
    n_final_sample = 5000  # number of samples to collect from variational family
    max_steps = 50000  # number of training iterations

    if _FIT_VI_MODELS:
        for family_name in ["sgpr", "mfvi"]:
            os.makedirs("{}/{}".format(_SAVE_ADDR_PREFIX, family_name), exist_ok=True)

            if family_name == "mfvi":
                family_name_full = "Mean-field VI"
                ensemble_variational_family = adaptive_ensemble.variational_mfvi
                ensemble_variational_family_sample = adaptive_ensemble.variational_mfvi_sample
            elif family_name == "sgpr":
                family_name_full = "Sparse Gaussian Process"
                ensemble_variational_family = adaptive_ensemble.variational_sgpr
                ensemble_variational_family_sample = adaptive_ensemble.variational_sgpr_sample

            """ 3.2. Set up the computational graph """
            vi_graph = tf.Graph()

            with vi_graph.as_default():
                # sample from variational family
                (weight_gp_dict, resid_gp, temp_dict, sigma, _, _,  # variational RVs
                 weight_gp_mean_dict, weight_gp_vcov_dict,  # variational parameters, weight GP
                 resid_gp_mean, resid_gp_vcov,  # resid GP variational parameters
                 temp_mean_dict, temp_sdev_dict,  # temperature variational parameters
                 sigma_mean, sigma_sdev,  # variational parameters, resid GP
                 ) = ensemble_variational_family(X=X_test,
                                                 Z=X_induce,
                                                 base_pred=base_test_pred,
                                                 family_tree=family_tree_dict,
                                                 log_ls_weight=LOG_LS_WEIGHT_HMC,
                                                 log_ls_resid=LOG_LS_RESID_HMC,
                                                 kernel_func=gp.rbf,
                                                 ridge_factor=1e-3)

                # assemble kwargs for make_value_setter
                variational_rv_dict = {"ensemble_resid": resid_gp, "sigma": sigma, }
                variational_rv_dict.update(temp_dict)
                variational_rv_dict.update(weight_gp_dict)

                # compute the expected predictive log-likelihood
                with ed.tape() as model_tape:
                    with ed.interception(make_value_setter(**variational_rv_dict)):
                        y = adaptive_ensemble.model_tailfree(X=X_test, base_pred=base_test_pred,
                                                             family_tree=family_tree_dict,
                                                             kernel_func=gp.rbf,
                                                             log_ls_weight=LOG_LS_WEIGHT_HMC,
                                                             log_ls_resid=LOG_LS_RESID_HMC,
                                                             ridge_factor=1e-3)

                log_likelihood = y.distribution.log_prob(y_test)

                # compute the KL divergence
                kl = 0.
                for rv_name, variational_rv in variational_rv_dict.items():
                    kl += tf.reduce_sum(
                        variational_rv.distribution.kl_divergence(
                            model_tape[rv_name].distribution)
                    )

                # define loss op: ELBO = E_q(p(x|z)) + KL(q || p)
                elbo = tf.reduce_mean(log_likelihood - kl)

                # define optimizer
                optimizer = tf.train.AdagradOptimizer(1e-1)
                train_op = optimizer.minimize(-elbo)

                # define init op
                init_op = tf.global_variables_initializer()

                vi_graph.finalize()

            """ 3.3. execute optimization, then sample from variational family """

            # optimization
            with tf.Session(graph=vi_graph) as sess:
                start_time = time.time()

                sess.run(init_op)
                for step in range(max_steps):
                    start_time = time.time()
                    _, elbo_value, = sess.run([train_op, elbo])
                    if step % 1000 == 0:
                        duration = time.time() - start_time
                        print("Step: {:>3d} ELBO: {:.3f}, ({:.3f} sec)".format(
                            step, elbo_value, duration))

                (weight_gp_mean_dict_val, weight_gp_vcov_dict_val,
                 resid_gp_mean_val, resid_gp_vcov_val,
                 temp_mean_dict_val, temp_sdev_dict_val,
                 sigma_mean_val, sigma_sdev_val,) = sess.run([
                    weight_gp_mean_dict, weight_gp_vcov_dict,
                    resid_gp_mean, resid_gp_vcov,
                    temp_mean_dict, temp_sdev_dict,  # temperature variational parameters
                    sigma_mean, sigma_sdev,
                ])

                sess.close()

            # sample
            # Note: here we filled in arbitrary parameters for ls samples -
            # we don't need the output ls_resid and ls_weight parameters
            # since these parameters are fix during VI and was never inferred.
            with tf.Session() as sess:
                (weight_gp_sample_dict, temp_sample_dict,
                 resid_gp_sample, sigma_sample, _, _) = (
                    ensemble_variational_family_sample(
                        n_final_sample,
                        weight_gp_mean_dict_val, weight_gp_vcov_dict_val,
                        temp_mean_dict_val, temp_sdev_dict_val,
                        resid_gp_mean_val, resid_gp_vcov_val,
                        sigma_mean_val, sigma_sdev_val,
                        log_ls_weight_mean=LOG_LS_WEIGHT_HMC,
                        log_ls_weight_sdev=.01,
                        log_ls_resid_mean=LOG_LS_RESID_HMC,
                        log_ls_resid_sdev=.01
                    ))

                (weight_gp_sample_dict_val, temp_sample_dict_val,
                 resid_gp_sample_val, sigma_sample_val,
                 ) = sess.run([
                    weight_gp_sample_dict, temp_sample_dict,
                    resid_gp_sample, sigma_sample
                ])

            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/sigma_sample.pkl'.format(family_name)), 'wb') as file:
                pk.dump(sigma_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)
            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/temp_sample.pkl'.format(family_name)), 'wb') as file:
                pk.dump(temp_sample_dict_val, file, protocol=pk.HIGHEST_PROTOCOL)
            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/weight_sample.pkl'.format(family_name)), 'wb') as file:
                pk.dump(weight_gp_sample_dict_val, file, protocol=pk.HIGHEST_PROTOCOL)
            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/ensemble_resid_sample.pkl'.format(family_name)), 'wb') as file:
                pk.dump(resid_gp_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)

            """ 3.5. prediction and posterior sampling """

            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/sigma_sample.pkl'.format(family_name)), 'rb') as file:
                sigma_sample_val = pk.load(file)
            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/temp_sample.pkl'.format(family_name)), 'rb') as file:
                temp_sample_dict_val = pk.load(file)
            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/weight_sample.pkl'.format(family_name)), 'rb') as file:
                weight_gp_sample_dict_val = pk.load(file)
            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/ensemble_resid_sample.pkl'.format(family_name)), 'rb') as file:
                resid_gp_sample_val = pk.load(file)

            # compute GP prediction for weight GP and residual GP
            raw_weights_dict = dict()

            for model_name, model_weight_sample in weight_gp_sample_dict_val.items():
                # extract node name and verify correctness
                node_name = model_name.replace("{}_".format(tail_free.BASE_WEIGHT_NAME_PREFIX), "")
                assert node_name in tail_free.get_nonroot_node_names(family_tree_dict)

                raw_weights_dict[node_name] = (
                    gp.sample_posterior_full(X_new=X_valid, X=X_test,
                                             f_sample=model_weight_sample.T,
                                             ls=np.exp(LOG_LS_WEIGHT_HMC),
                                             kernel_func=gp.rbf).T.astype(np.float32))

            ensemble_resid_valid_sample = (
                gp.sample_posterior_full(X_new=X_valid, X=X_test,
                                         f_sample=resid_gp_sample_val.T,
                                         ls=np.exp(LOG_LS_RESID_HMC),
                                         kernel_func=gp.rbf).T
            )

            # prepare temperature dictionary
            parent_temp_dict = dict()
            for model_name, parent_temp_sample in temp_sample_dict_val.items():
                # extract node name and verify correctness
                node_name = model_name.replace("{}_".format(tail_free.TEMP_NAME_PREFIX), "")
                assert node_name in tail_free.get_parent_node_names(family_tree_dict)

                parent_temp_dict[node_name] = parent_temp_sample

            # compute sample for posterior mean
            (ensemble_sample_val, ensemble_mean_val,
             ensemble_weights_val, cond_weights_dict_val, ensemble_model_names) = (
                adaptive_ensemble.sample_posterior_tailfree(X=X_valid, base_pred_dict=base_valid_pred,
                                                            family_tree=family_tree_dict,
                                                            weight_gp_dict=raw_weights_dict,
                                                            temp_dict=parent_temp_dict,
                                                            resid_gp_sample=ensemble_resid_valid_sample,
                                                            log_ls_weight=LOG_LS_WEIGHT_HMC))

            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/ensemble_posterior_mean_sample.pkl'.format(family_name)), 'wb') as file:
                pk.dump(ensemble_mean_val, file, protocol=pk.HIGHEST_PROTOCOL)

            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/ensemble_posterior_dist_sample.pkl'.format(family_name)), 'wb') as file:
                pk.dump(ensemble_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)

            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/ensemble_posterior_node_weight_dict.pkl'.format(family_name)), 'wb') as file:
                pk.dump(cond_weights_dict_val, file, protocol=pk.HIGHEST_PROTOCOL)

            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/ensemble_posterior_model_weights.pkl'.format(family_name)), 'wb') as file:
                pk.dump(ensemble_weights_val, file, protocol=pk.HIGHEST_PROTOCOL)

            """ 3.5.2. visualize: base prediction """
            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/ensemble_posterior_mean_sample.pkl'.format(family_name)), 'rb') as file:
                ensemble_mean_val = pk.load(file)

            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/ensemble_posterior_dist_sample.pkl'.format(family_name)), 'rb') as file:
                ensemble_sample_val = pk.load(file)

            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/ensemble_posterior_node_weight_dict.pkl'.format(family_name)), 'rb') as file:
                cond_weights_dict_val = pk.load(file)

            with open(os.path.join(_SAVE_ADDR_PREFIX,
                                   '{}/ensemble_posterior_model_weights.pkl'.format(family_name)), 'rb') as file:
                ensemble_weights_val = pk.load(file)

            """ 3.5.3. visualize: ensemble posterior predictive mean """

            posterior_mean_mu = np.nanmean(ensemble_mean_val, axis=0)
            posterior_mean_cov = np.nanvar(ensemble_mean_val, axis=0)

            posterior_mean_median = np.nanmedian(ensemble_mean_val, axis=0)
            posterior_mean_quantiles = [
                np.percentile(ensemble_mean_val,
                              [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
                for q in [68, 95, 99]
            ]

            visual_util.gpr_2d_visual(y_valid,
                                      pred_cov=posterior_mean_cov,
                                      X_train=X_test, y_train=y_test,
                                      X_test=X_valid, y_test=y_valid,
                                      title="True Surface",
                                      save_addr=os.path.join(
                                          _SAVE_ADDR_PREFIX, "ground_truth.png")
                                      )

            visual_util.gpr_2d_visual(posterior_mean_mu,
                                      pred_cov=posterior_mean_cov,
                                      X_train=X_test, y_train=y_test,
                                      X_test=X_valid, y_test=y_valid,
                                      title="Ensemble Posterior Mean, {}".format(family_name_full),
                                      save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                             "{}/ensemble_posterior_mean.png".format(family_name))
                                      )

            visual_util.gpr_2d_visual(posterior_mean_median,
                                      pred_cov=None,
                                      X_train=X_test, y_train=y_test,
                                      X_test=X_valid, y_test=y_valid,
                                      title="Ensemble Posterior Median, {}".format(family_name_full),
                                      save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                             "{}/ensemble_posterior_median.png".format(family_name))
                                      )

            """ 3.5.4. visualize: ensemble residual """

            posterior_resid_mu = np.nanmean(ensemble_resid_valid_sample, axis=0)
            posterior_resid_cov = np.nanvar(ensemble_resid_valid_sample, axis=0)

            posterior_resid_median = np.nanmedian(ensemble_resid_valid_sample, axis=0)
            posterior_resid_quantiles = [
                np.percentile(ensemble_resid_valid_sample,
                              [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
                for q in [68, 95, 99]
            ]

            visual_util.gpr_2d_visual(posterior_resid_mu,
                                      pred_cov=posterior_resid_cov,
                                      X_train=X_test, y_train=y_test,
                                      X_test=X_valid, y_test=y_valid,
                                      title="Ensemble Posterior Residual, {}".format(family_name_full),
                                      save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                             "{}/ensemble_posterior_resid.png".format(family_name))
                                      )

            visual_util.gpr_2d_visual(posterior_resid_median,
                                      pred_cov=None,
                                      X_train=X_test, y_train=y_test,
                                      X_test=X_valid, y_test=y_valid,
                                      title="Ensemble Posterior Residual, {}".format(family_name_full),
                                      save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                             "{}/ensemble_posterior_resid_median.png".format(
                                                                 family_name))
                                      )

            visual_util.gpr_2d_visual(np.sqrt(posterior_resid_cov),
                                      pred_cov=None,
                                      X_train=X_test, y_train=y_test * 0,
                                      X_test=X_valid, y_test=np.sqrt(posterior_resid_cov),
                                      title="Ensemble Posterior Residual Uncertainty, {}".format(family_name_full),
                                      save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                             "{}/ensemble_posterior_resid_cov.png".format(family_name))
                                      )

            """ 3.5.5. visualize: ensemble posterior full """

            posterior_dist_mu = np.nanmean(ensemble_sample_val, axis=0)
            posterior_dist_cov = np.nanvar(ensemble_sample_val, axis=0)

            posterior_dist_median = np.nanmedian(ensemble_sample_val, axis=0)
            posterior_dist_quantiles = [
                np.percentile(ensemble_sample_val,
                              [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
                for q in [68, 95, 99]
            ]

            visual_util.gpr_2d_visual(posterior_dist_mu,
                                      pred_cov=posterior_dist_cov,
                                      X_train=X_test, y_train=y_test,
                                      X_test=X_valid, y_test=y_valid,
                                      title="Ensemble Posterior Predictive, {}".format(family_name_full),
                                      save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                             "{}/ensemble_posterior_full.png".format(family_name))
                                      )

            visual_util.gpr_2d_visual(posterior_dist_median,
                                      pred_cov=posterior_dist_cov,
                                      X_train=X_test, y_train=y_test,
                                      X_test=X_valid, y_test=y_valid,
                                      title="Ensemble Posterior Predictive Median, {}".format(family_name_full),
                                      save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                             "{}/ensemble_posterior_full_median.png".format(
                                                                 family_name))
                                      )

            visual_util.gpr_2d_visual(np.sqrt(posterior_dist_cov),
                                      pred_cov=None,
                                      X_train=X_test, y_train=y_test * 0,
                                      X_test=X_valid, y_test=np.sqrt(posterior_dist_cov),
                                      title="Ensemble Posterior Residual Uncertainty, {}".format(family_name_full),
                                      save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                             "{}/ensemble_posterior_resid_cov.png".format(family_name))
                                      )

            """ 3.5.6. visualize: ensemble posterior reliability """
            y_calib = y_valid[calib_sample_id]
            y_sample_calib = ensemble_sample_val[:, calib_sample_id].T

            # cdf_eval = np.mean(y_sample_calib < np.expand_dims(y_calib, -1), -1)
            # cdf_perc = np.mean((cdf_eval.reshape(len(cdf_eval), 1) <
            #                     cdf_eval.reshape(1, len(cdf_eval))), 0)

            visual_util.prob_calibration_1d(
                y_calib, y_sample_calib,
                title="Ensemble, {}".format(family_name_full),
                save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                       "{}/ensemble_calibration_prob.png".format(family_name)))

            """ 3.5.7. visualize: base ensemble weight with uncertainty """
            visual_util.plot_ensemble_weight_mean_2d(X=X_valid, weight_sample=ensemble_weights_val,
                                                     model_names=ensemble_model_names,
                                                     save_addr_prefix=os.path.join(
                                                         _SAVE_ADDR_PREFIX, "{}/".format(family_name)))

            # # model family weights
            # ensemble_weights_family = np.stack(
            #     [cond_weights_dict_val[key] for key in family_tree_dict['root']], axis=-1)
            # visual_util.plot_ensemble_weight_mean_1d(X=X_valid,
            #                                          weight_sample=ensemble_weights_family,
            #                                          model_names=family_tree_dict['root'],
            #                                          save_addr_prefix=os.path.join(
            #                                              _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))
            # visual_util.plot_ensemble_weight_median_1d(X=X_valid,
            #                                            weight_sample=ensemble_weights_family,
            #                                            model_names=family_tree_dict['root'],
            #                                            save_addr_prefix=os.path.join(
            #                                                _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))

    """""""""""""""""""""""""""""""""
    # 4. Variational Inference, Augmened Objective
    """""""""""""""""""""""""""""""""
    with open(os.path.join(_SAVE_ADDR_PREFIX,
                           'hmc/ensemble_ls_resid_sample.pkl'), 'rb') as file:
        ls_weight_sample_val = pk.load(file)
    with open(os.path.join(_SAVE_ADDR_PREFIX,
                           'hmc/ensemble_ls_weight_sample.pkl'), 'rb') as file:
        ls_resid_sample_val = pk.load(file)

    with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_test_pred.pkl'), 'rb') as file:
        base_test_pred = pk.load(file)

    with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_valid_pred.pkl'), 'rb') as file:
        base_valid_pred = pk.load(file)

    LOG_LS_WEIGHT_HMC = np.median(ls_weight_sample_val)
    LOG_LS_RESID_HMC = np.median(ls_resid_sample_val)

    """ 3.1. basic data/algorithm config"""

    X_induce = KMeans(n_clusters=10, random_state=100).fit(
        X_valid[calib_sample_id]).cluster_centers_.astype(np.float32)
    family_tree_dict = {tail_free.ROOT_NODE_DEFAULT_NAME:
                            list(base_test_pred.keys())}  # use default dictionary for now.

    n_inference_sample = 100
    n_final_sample = 5000  # number of samples to collect from variational family
    max_steps = 50000  # number of training iterations

    for family_name in ["sgpr_crps", "mfvi_crps"]:
        os.makedirs("{}/{}".format(_SAVE_ADDR_PREFIX, family_name), exist_ok=True)

        if "mfvi" in family_name:
            family_name_full = "Mean-field VI"
            ensemble_variational_family = adaptive_ensemble.variational_mfvi
            ensemble_variational_family_sample = adaptive_ensemble.variational_mfvi_sample
        elif "sgpr" in family_name:
            family_name_full = "Sparse Gaussian Process"
            ensemble_variational_family = adaptive_ensemble.variational_sgpr
            ensemble_variational_family_sample = adaptive_ensemble.variational_sgpr_sample

        """ 3.2. Set up the computational graph """
        vi_graph = tf.Graph()

        with vi_graph.as_default():
            # sample from variational family
            (weight_gp_dict, resid_gp, temp_dict, sigma, _, _,  # variational RVs
             weight_gp_mean_dict, weight_gp_vcov_dict,  # variational parameters, weight GP
             resid_gp_mean, resid_gp_vcov,  # resid GP variational parameters
             temp_mean_dict, temp_sdev_dict,  # temperature variational parameters
             sigma_mean, sigma_sdev,  # variational parameters, resid GP
             ) = ensemble_variational_family(X=X_test,
                                             Z=X_induce,
                                             base_pred=base_test_pred,
                                             family_tree=family_tree_dict,
                                             log_ls_weight=LOG_LS_WEIGHT_HMC,
                                             log_ls_resid=LOG_LS_RESID_HMC,
                                             kernel_func=gp.rbf,
                                             ridge_factor=1e-3)

            # assemble kwargs for make_value_setter
            variational_rv_dict = {"ensemble_resid": resid_gp, "sigma": sigma, }
            variational_rv_dict.update(temp_dict)
            variational_rv_dict.update(weight_gp_dict)

            # compute the expected predictive log-likelihood
            with ed.tape() as model_tape:
                with ed.interception(make_value_setter(**variational_rv_dict)):
                    y = adaptive_ensemble.model_tailfree(X=X_test, base_pred=base_test_pred,
                                                         family_tree=family_tree_dict,
                                                         kernel_func=gp.rbf,
                                                         log_ls_weight=LOG_LS_WEIGHT_HMC,
                                                         log_ls_resid=LOG_LS_RESID_HMC,
                                                         ridge_factor=1e-3)

            log_likelihood = y.distribution.log_prob(y_test)

            # compute the KL divergence
            kl = 0.
            for rv_name, variational_rv in variational_rv_dict.items():
                kl += tf.reduce_sum(
                    variational_rv.distribution.kl_divergence(
                        model_tape[rv_name].distribution)
                )

            # define loss op: ELBO = E_q(p(x|z)) + KL(q || p)
            elbo = tf.reduce_mean(log_likelihood - kl)

            # compute the calibration score
            y_sample_1 = y.distribution.sample(n_inference_sample)
            y_sample_2 = y.distribution.sample(n_inference_sample)

            crps_loss = score.make_kernel_score_loss(
                X_sample=y_sample_1,
                Y_sample=y_sample_2,
                Y_obs=np.expand_dims(y_test, -1),
                log_prob=y.distribution.log_prob,
                dist_func=tf.abs
            )

            # define loss objective to maximize: ELBO = E_q(-log p(x|z)) - KL(q || p)
            loss_op = -elbo + 25 * crps_loss  # -elbo

            # define optimizer
            optimizer = tf.train.AdagradOptimizer(1e-1)
            train_op = optimizer.minimize(loss_op)

            # define init op
            init_op = tf.global_variables_initializer()

            vi_graph.finalize()

        """ 3.3. execute optimization, then sample from variational family """

        # optimization
        with tf.Session(graph=vi_graph) as sess:
            start_time = time.time()

            sess.run(init_op)
            for step in range(max_steps):
                start_time = time.time()
                _, elbo_value, crps_val = sess.run([
                    train_op, elbo, crps_loss])
                if step % 1000 == 0:
                    duration = time.time() - start_time
                    print("Step: {:>3d} ELBO: {:.3f}, Calibration: {:.3f}, ({:.3f} sec)".format(
                        step, elbo_value, crps_val, duration))

            (weight_gp_mean_dict_val, weight_gp_vcov_dict_val,
             resid_gp_mean_val, resid_gp_vcov_val,
             temp_mean_dict_val, temp_sdev_dict_val,
             sigma_mean_val, sigma_sdev_val,) = sess.run([
                weight_gp_mean_dict, weight_gp_vcov_dict,
                resid_gp_mean, resid_gp_vcov,
                temp_mean_dict, temp_sdev_dict,  # temperature variational parameters
                sigma_mean, sigma_sdev,
            ])

            sess.close()

        # sample
        # Note: here we filled in arbitrary parameters for ls samples -
        # we don't need the output ls_resid and ls_weight parameters
        # since these parameters are fix during VI and was never inferred.
        with tf.Session() as sess:
            (weight_gp_sample_dict, temp_sample_dict,
             resid_gp_sample, sigma_sample, _, _) = (
                ensemble_variational_family_sample(
                    n_final_sample,
                    weight_gp_mean_dict_val, weight_gp_vcov_dict_val,
                    temp_mean_dict_val, temp_sdev_dict_val,
                    resid_gp_mean_val, resid_gp_vcov_val,
                    sigma_mean_val, sigma_sdev_val,
                    log_ls_weight_mean=LOG_LS_WEIGHT_HMC,
                    log_ls_weight_sdev=.01,
                    log_ls_resid_mean=LOG_LS_RESID_HMC,
                    log_ls_resid_sdev=.01
                ))

            (weight_gp_sample_dict_val, temp_sample_dict_val,
             resid_gp_sample_val, sigma_sample_val,
             ) = sess.run([
                weight_gp_sample_dict, temp_sample_dict,
                resid_gp_sample, sigma_sample
            ])

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/sigma_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(sigma_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/temp_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(temp_sample_dict_val, file, protocol=pk.HIGHEST_PROTOCOL)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/weight_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(weight_gp_sample_dict_val, file, protocol=pk.HIGHEST_PROTOCOL)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_resid_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(resid_gp_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)

        """ 3.5. prediction and posterior sampling """

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/sigma_sample.pkl'.format(family_name)), 'rb') as file:
            sigma_sample_val = pk.load(file)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/temp_sample.pkl'.format(family_name)), 'rb') as file:
            temp_sample_dict_val = pk.load(file)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/weight_sample.pkl'.format(family_name)), 'rb') as file:
            weight_gp_sample_dict_val = pk.load(file)
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_resid_sample.pkl'.format(family_name)), 'rb') as file:
            resid_gp_sample_val = pk.load(file)

        # compute GP prediction for weight GP and residual GP
        raw_weights_dict = dict()

        for model_name, model_weight_sample in weight_gp_sample_dict_val.items():
            # extract node name and verify correctness
            node_name = model_name.replace("{}_".format(tail_free.BASE_WEIGHT_NAME_PREFIX), "")
            assert node_name in tail_free.get_nonroot_node_names(family_tree_dict)

            raw_weights_dict[node_name] = (
                gp.sample_posterior_full(X_new=X_valid, X=X_test,
                                         f_sample=model_weight_sample.T,
                                         ls=np.exp(LOG_LS_WEIGHT_HMC),
                                         kernel_func=gp.rbf).T.astype(np.float32))

        ensemble_resid_valid_sample = (
            gp.sample_posterior_full(X_new=X_valid, X=X_test,
                                     f_sample=resid_gp_sample_val.T,
                                     ls=np.exp(LOG_LS_RESID_HMC),
                                     kernel_func=gp.rbf).T
        )

        # prepare temperature dictionary
        parent_temp_dict = dict()
        for model_name, parent_temp_sample in temp_sample_dict_val.items():
            # extract node name and verify correctness
            node_name = model_name.replace("{}_".format(tail_free.TEMP_NAME_PREFIX), "")
            assert node_name in tail_free.get_parent_node_names(family_tree_dict)

            parent_temp_dict[node_name] = parent_temp_sample

        # compute sample for posterior mean
        (ensemble_sample_val, ensemble_mean_val,
         ensemble_weights_val, cond_weights_dict_val, ensemble_model_names) = (
            adaptive_ensemble.sample_posterior_tailfree(X=X_valid, base_pred_dict=base_valid_pred,
                                                        family_tree=family_tree_dict,
                                                        weight_gp_dict=raw_weights_dict,
                                                        temp_dict=parent_temp_dict,
                                                        resid_gp_sample=ensemble_resid_valid_sample,
                                                        log_ls_weight=LOG_LS_WEIGHT_HMC))

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_mean_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(ensemble_mean_val, file, protocol=pk.HIGHEST_PROTOCOL)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_dist_sample.pkl'.format(family_name)), 'wb') as file:
            pk.dump(ensemble_sample_val, file, protocol=pk.HIGHEST_PROTOCOL)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_node_weight_dict.pkl'.format(family_name)), 'wb') as file:
            pk.dump(cond_weights_dict_val, file, protocol=pk.HIGHEST_PROTOCOL)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_model_weights.pkl'.format(family_name)), 'wb') as file:
            pk.dump(ensemble_weights_val, file, protocol=pk.HIGHEST_PROTOCOL)

        """ 3.5.2. visualize: base prediction """
        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_mean_sample.pkl'.format(family_name)), 'rb') as file:
            ensemble_mean_val = pk.load(file)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_dist_sample.pkl'.format(family_name)), 'rb') as file:
            ensemble_sample_val = pk.load(file)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_node_weight_dict.pkl'.format(family_name)), 'rb') as file:
            cond_weights_dict_val = pk.load(file)

        with open(os.path.join(_SAVE_ADDR_PREFIX,
                               '{}/ensemble_posterior_model_weights.pkl'.format(family_name)), 'rb') as file:
            ensemble_weights_val = pk.load(file)

        """ 3.5.3. visualize: ensemble posterior predictive mean """

        posterior_mean_mu = np.nanmean(ensemble_mean_val, axis=0)
        posterior_mean_cov = np.nanvar(ensemble_mean_val, axis=0)

        posterior_mean_median = np.nanmedian(ensemble_mean_val, axis=0)
        posterior_mean_quantiles = [
            np.percentile(ensemble_mean_val,
                          [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
            for q in [68, 95, 99]
        ]

        visual_util.gpr_2d_visual(y_valid,
                                  pred_cov=posterior_mean_cov,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="True Surface",
                                  save_addr=os.path.join(
                                      _SAVE_ADDR_PREFIX, "ground_truth.png")
                                  )

        visual_util.gpr_2d_visual(posterior_mean_mu,
                                  pred_cov=posterior_mean_cov,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Mean, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_mean.png".format(family_name))
                                  )

        visual_util.gpr_2d_visual(posterior_mean_median,
                                  pred_cov=None,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Median, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_median.png".format(family_name))
                                  )

        """ 3.5.4. visualize: ensemble residual """

        posterior_resid_mu = np.nanmean(ensemble_resid_valid_sample, axis=0)
        posterior_resid_cov = np.nanvar(ensemble_resid_valid_sample, axis=0)

        posterior_resid_median = np.nanmedian(ensemble_resid_valid_sample, axis=0)
        posterior_resid_quantiles = [
            np.percentile(ensemble_resid_valid_sample,
                          [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
            for q in [68, 95, 99]
        ]

        visual_util.gpr_2d_visual(posterior_resid_mu,
                                  pred_cov=posterior_resid_cov,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Residual, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_resid.png".format(family_name))
                                  )

        visual_util.gpr_2d_visual(posterior_resid_median,
                                  pred_cov=None,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Residual, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_resid_median.png".format(family_name))
                                  )

        visual_util.gpr_2d_visual(np.sqrt(posterior_resid_cov),
                                  pred_cov=None,
                                  X_train=X_test, y_train=y_test * 0,
                                  X_test=X_valid, y_test=np.sqrt(posterior_resid_cov),
                                  title="Ensemble Posterior Residual Uncertainty, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_resid_cov.png".format(family_name))
                                  )

        """ 3.5.5. visualize: ensemble posterior full """

        posterior_dist_mu = np.nanmean(ensemble_sample_val, axis=0)
        posterior_dist_cov = np.nanvar(ensemble_sample_val, axis=0)

        posterior_dist_median = np.nanmedian(ensemble_sample_val, axis=0)
        posterior_dist_quantiles = [
            np.percentile(ensemble_sample_val,
                          [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
            for q in [68, 95, 99]
        ]

        visual_util.gpr_2d_visual(posterior_dist_mu,
                                  pred_cov=posterior_dist_cov,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Predictive, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_full.png".format(family_name))
                                  )

        visual_util.gpr_2d_visual(posterior_dist_median,
                                  pred_cov=posterior_dist_cov,
                                  X_train=X_test, y_train=y_test,
                                  X_test=X_valid, y_test=y_valid,
                                  title="Ensemble Posterior Predictive Median, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_full_median.png".format(family_name))
                                  )

        visual_util.gpr_2d_visual(np.sqrt(posterior_dist_cov),
                                  pred_cov=None,
                                  X_train=X_test, y_train=y_test * 0,
                                  X_test=X_valid, y_test=np.sqrt(posterior_dist_cov),
                                  title="Ensemble Posterior Residual Uncertainty, {}".format(family_name_full),
                                  save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                         "{}/ensemble_posterior_resid_cov.png".format(family_name))
                                  )

        """ 3.5.6. visualize: ensemble posterior reliability """
        y_calib = y_valid[calib_sample_id]
        y_sample_calib = ensemble_sample_val[:, calib_sample_id].T

        # cdf_eval = np.mean(y_sample_calib < np.expand_dims(y_calib, -1), -1)
        # cdf_perc = np.mean((cdf_eval.reshape(len(cdf_eval), 1) <
        #                     cdf_eval.reshape(1, len(cdf_eval))), 0)

        visual_util.prob_calibration_1d(
            y_calib, y_sample_calib,
            title="Ensemble, {}".format(family_name_full),
            save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                   "{}/ensemble_calibration_prob.png".format(family_name)))

        """ 3.5.7. visualize: base ensemble weight with uncertainty """
        visual_util.plot_ensemble_weight_mean_2d(X=X_valid, weight_sample=ensemble_weights_val,
                                                 model_names=ensemble_model_names,
                                                 save_addr_prefix=os.path.join(
                                                     _SAVE_ADDR_PREFIX, "{}/".format(family_name)))

        # # model family weights
        # ensemble_weights_family = np.stack(
        #     [cond_weights_dict_val[key] for key in family_tree_dict['root']], axis=-1)
        # visual_util.plot_ensemble_weight_mean_1d(X=X_valid,
        #                                          weight_sample=ensemble_weights_family,
        #                                          model_names=family_tree_dict['root'],
        #                                          save_addr_prefix=os.path.join(
        #                                              _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))
        # visual_util.plot_ensemble_weight_median_1d(X=X_valid,
        #                                            weight_sample=ensemble_weights_family,
        #                                            model_names=family_tree_dict['root'],
        #                                            save_addr_prefix=os.path.join(
        #                                                _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))


"""""""""""""""""""""""""""""""""
# 5. Nonparametric Calibration I: Isotonic Regression
"""""""""""""""""""""""""""""""""

# TODO(jereliu): to complete after 1D example

# load hyper-parameters
with open(os.path.join(_SAVE_ADDR_PREFIX,
                       'hmc/ensemble_ls_resid_sample.pkl'), 'rb') as file:
    ls_weight_sample_val = pk.load(file)
with open(os.path.join(_SAVE_ADDR_PREFIX,
                       'hmc/ensemble_ls_weight_sample.pkl'), 'rb') as file:
    ls_resid_sample_val = pk.load(file)

with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_test_pred.pkl'), 'rb') as file:
    base_test_pred = pk.load(file)

with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_valid_pred.pkl'), 'rb') as file:
    base_valid_pred = pk.load(file)

LOG_LS_WEIGHT_HMC = np.median(ls_weight_sample_val)
LOG_LS_RESID_HMC = np.median(ls_resid_sample_val)

X_induce = KMeans(n_clusters=10, random_state=100).fit(
    X_valid[calib_sample_id]).cluster_centers_.astype(np.float32)
family_tree_dict = {tail_free.ROOT_NODE_DEFAULT_NAME:
                        list(base_test_pred.keys())}  # use default dictionary for now.

n_inference_sample = 100
n_final_sample = 5000  # number of samples to collect from variational family
max_steps = 50000  # number of training iterations

family_name = "mfvi"
os.makedirs("{}/{}".format(_SAVE_ADDR_PREFIX, family_name), exist_ok=True)

if family_name == "mfvi":
    family_name_full = "Mean-field VI"
    ensemble_variational_family = adaptive_ensemble.variational_mfvi
    ensemble_variational_family_sample = adaptive_ensemble.variational_mfvi_sample
elif family_name == "sgpr":
    family_name_full = "Sparse Gaussian Process"
    ensemble_variational_family = adaptive_ensemble.variational_sgpr
    ensemble_variational_family_sample = adaptive_ensemble.variational_sgpr_sample

# load estimates
with open(os.path.join(_SAVE_ADDR_PREFIX,
                       '{}/ensemble_posterior_dist_sample.pkl'.format(family_name)), 'rb') as file:
    ensemble_sample_val = pk.load(file)

# build calibration dataset
y_calib = y_valid[calib_sample_id]
y_sample_calib = ensemble_sample_val[:, calib_sample_id].T

calib_data = calib_util.build_calibration_dataset(
    Y_obs=y_calib, Y_sample=y_sample_calib)


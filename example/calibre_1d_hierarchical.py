"""Calibre (Adaptive Ensemble) with hierarchical structure using MCMC and Penalized VI. """
import os
import time

from importlib import reload

import pickle as pk

import numpy as np

from sklearn.cluster import KMeans
from sklearn.isotonic import IsotonicRegression

import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow_probability import edward2 as ed

# sys.path.extend([os.getcwd()])

from calibre.model import gaussian_process as gp
from calibre.model import tailfree_process as tail_free
from calibre.model import gp_regression_monotone as gpr_mono
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

# TODO(jereliu): adjust VI for ls_weight/ls_resid prior
tfd = tfp.distributions

_FIT_BASE_MODELS = False
_PLOT_COMPOSITION = False

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

DEFAULT_LOG_LS_WEIGHT = np.log(0.15).astype(np.float32)
DEFAULT_LOG_LS_RESID = np.log(0.2).astype(np.float32)

"""""""""""""""""""""""""""""""""
# 1. Generate data
"""""""""""""""""""""""""""""""""

N_train = 20
N_test = 20
N_valid = 500

_SAVE_ADDR_PREFIX = "./result/calibre_1d_tree"
data_range = [0., 1.]
X_train, y_train = data_util.generate_1d_data(N=N_train, f=sin_curve_1d,
                                              noise_sd=0.03, seed=1000,
                                              uniform_x=True,
                                              uniform_x_range=data_range)
X_test, y_test = data_util.generate_1d_data(N=N_test, f=sin_curve_1d,
                                            noise_sd=0.03, seed=1500,
                                            uniform_x=True,
                                            uniform_x_range=data_range)

X_train = np.expand_dims(X_train, 1).astype(np.float32)
y_train = y_train.astype(np.float32)
X_test = np.expand_dims(X_test, 1).astype(np.float32)
y_test = y_test.astype(np.float32)

std_y_train = np.std(y_train)

X_valid = np.expand_dims(np.linspace(-0.5, 1.5, N_valid), 1).astype(np.float32)
y_valid = sin_curve_1d(X_valid)

N, D = X_train.shape

np.random.seed(100)
calib_sample_id = np.where((X_valid > data_range[0]) &
                           (X_valid <= data_range[1]))[0]
calib_sample_id = np.random.choice(len(calib_sample_id),
                                   size=len(calib_sample_id))

#
plt.plot(np.linspace(-0.5, 1.5, 100),
         sin_curve_1d(np.linspace(-0.5, 1.5, 100)), c='black')
plt.plot(X_train.squeeze(), y_train.squeeze(),
         'o', c='red', markeredgecolor='black')
plt.plot(X_test.squeeze(), y_test.squeeze(),
         'o', c='blue', markeredgecolor='black')
plt.close()

""" 1.1. Build base GP models using GPflow """
if _FIT_BASE_MODELS:
    gpf_util.fit_base_gp_models(X_train, y_train,
                                X_test, y_test,
                                X_valid, y_valid,
                                kern_func_dict=DEFAULT_KERN_FUNC_DICT_GPFLOW,
                                n_valid_sample=500,
                                save_addr_prefix="{}/base".format(_SAVE_ADDR_PREFIX),
                                )

"""""""""""""""""""""""""""""""""
# 2. MCMC
"""""""""""""""""""""""""""""""""
family_name = "hmc"
family_name_full = "Hamilton MC"

_SAVE_ADDR = _SAVE_ADDR_PREFIX
with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_test_pred.pkl'), 'rb') as file:
    base_test_pred = pk.load(file)

with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_valid_pred.pkl'), 'rb') as file:
    base_valid_pred = pk.load(file)

"""2.1. sampler basic config"""
y_test = y_test[:100].astype(np.float32)
X_test = X_test[:100].astype(np.float32)

N = X_test.shape[0]
K = len(base_test_pred)

family_tree_dict = _EXAMPLE_DICTIONARY_SIMPLE

num_results = 1000
num_burnin_steps = 5000

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


    def target_log_prob_fn(sigma,
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
                         ls_weight=DEFAULT_LOG_LS_WEIGHT,
                         ls_resid=DEFAULT_LOG_LS_RESID,
                         sigma=sigma,
                         ensemble_resid=ensemble_resid,
                         **node_specific_kwargs)


    # set up state container
    initial_state = [
                        # tf.random_normal([N, K], stddev=0.01, name='init_ensemble_weight'),
                        # tf.random_normal([N], stddev=0.01, name='init_f_ensemble'),
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

    sigma_sample, ensemble_resid_sample = state[:2]
    temp_sample = state[2:2 + len(cond_weight_temp_names)]
    weight_sample = state[2 + len(cond_weight_temp_names):]

    # set up init op
    init_op = tf.global_variables_initializer()

    mcmc_graph.finalize()

""" 2.2. execute sampling"""
with tf.Session(graph=mcmc_graph) as sess:
    init_op.run()
    [
        sigma_sample_val,
        temp_sample_val,
        resid_sample_val,
        weight_sample_val,
        is_accepted_,
    ] = sess.run(
        [
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
                                 ls=np.exp(DEFAULT_LOG_LS_WEIGHT),
                                 kernel_func=gp.rbf).T.astype(np.float32)
    )

ensemble_resid_valid_sample = (
    gp.sample_posterior_full(X_new=X_valid, X=X_test,
                             f_sample=resid_sample_val.T,
                             ls=np.exp(DEFAULT_LOG_LS_RESID),
                             kernel_func=gp.rbf).T
)

# compute sample for posterior mean
raw_weights_dict = dict(zip(tail_free.get_nonroot_node_names(family_tree_dict),
                            model_weight_valid_sample))
parent_temp_dict = dict(zip(tail_free.get_parent_node_names(family_tree_dict),
                            temp_sample_val))

(ensemble_sample_val, ensemble_mean_val,
 ensemble_weights_val, cond_weights_dict_val, ensemble_model_names) = (
    adaptive_ensemble.sample_posterior_tailfree(X=X_valid, base_pred_dict=base_valid_pred, family_tree=family_tree_dict,
                                                weight_gp_dict=raw_weights_dict, temp_dict=parent_temp_dict,
                                                resid_gp_sample=ensemble_resid_valid_sample,
                                                log_ls_weight=DEFAULT_LOG_LS_WEIGHT))

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

""" 2.3.2. visualize: base prediction """
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

with open(os.path.join(_SAVE_ADDR_PREFIX,
                       '{}/ensemble_posterior_model_weights_corr.pkl'.format(family_name)), 'rb') as file:
    ensemble_weight_corr = pk.load(file)

base_pred_dict = {key: value for key, value in base_valid_pred.items()
                  if key in ensemble_model_names}

visual_util.plot_base_prediction(base_pred=base_pred_dict,
                                 X_valid=X_valid, y_valid=y_valid,
                                 X_train=X_train, y_train=y_train,
                                 save_addr=os.path.join(
                                     _SAVE_ADDR_PREFIX,
                                     "{}/ensemble_base_model_fit.png".format(family_name)))

""" 2.3.3. visualize: ensemble posterior predictive mean """

posterior_mean_mu = np.nanmean(ensemble_mean_val, axis=0)
posterior_mean_cov = np.nanvar(ensemble_mean_val, axis=0)

posterior_mean_median = np.nanmedian(ensemble_mean_val, axis=0)
posterior_mean_quantiles = [
    np.percentile(ensemble_mean_val,
                  [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
    for q in [68, 95, 99]
]

visual_util.gpr_1d_visual(posterior_mean_mu,
                          pred_cov=posterior_mean_cov,
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Mean, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/ensemble_posterior_mean.png".format(family_name))
                          )

visual_util.gpr_1d_visual(posterior_mean_median,
                          pred_cov=None,
                          pred_quantiles=posterior_mean_quantiles,
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Mean Quantiles, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/ensemble_posterior_mean_quantile.png".format(family_name))
                          )

visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                          pred_samples=list(ensemble_mean_val)[:2500],
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Samples, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/ensemble_posterior_sample.png".format(family_name))
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

visual_util.gpr_1d_visual(posterior_resid_mu,
                          pred_cov=posterior_resid_cov,
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Residual, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/ensemble_posterior_resid.png".format(family_name))
                          )

visual_util.gpr_1d_visual(posterior_resid_median,
                          pred_cov=None,
                          pred_quantiles=posterior_resid_quantiles,
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Residual Quantiles, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/ensemble_posterior_resid_quantile.png".format(family_name))
                          )

visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                          pred_samples=list(ensemble_resid_valid_sample)[:2500],
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Residual Samples, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/ensemble_posterior_resid_sample.png".format(family_name))
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

visual_util.gpr_1d_visual(posterior_dist_mu,
                          pred_cov=posterior_dist_cov,
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Predictive, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/ensemble_posterior_full.png".format(family_name))
                          )

visual_util.gpr_1d_visual(posterior_dist_median,
                          pred_cov=None,
                          pred_quantiles=posterior_dist_quantiles,
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Predictive Quantiles, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/ensemble_posterior_full_quantile.png".format(family_name))
                          )
visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                          pred_samples=list(ensemble_sample_val)[:2500],
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Predictive Samples, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/ensemble_posterior_full_sample.png".format(family_name))
                          )

visual_util.gpr_1d_visual(pred_mean=posterior_dist_median,
                          pred_cov=None,
                          pred_quantiles=[
                              [posterior_dist_median - 3 * np.sqrt(posterior_mean_cov),
                               posterior_dist_median + 3 * np.sqrt(posterior_mean_cov)],
                              [posterior_dist_median - 3 * np.sqrt(posterior_dist_cov),
                               posterior_dist_median + 3 * np.sqrt(posterior_dist_cov)],
                          ],
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          quantile_colors=["red", "black"],
                          quantile_alpha=0.2,
                          title="Ensemble Posterior, Uncertainty Decomposition, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/ensemble_posterior_unc_decomp.png".format(family_name))
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
visual_util.plot_ensemble_weight_mean_1d(X=X_valid, weight_sample=ensemble_weights_val,
                                         model_names=ensemble_model_names,
                                         save_addr_prefix=os.path.join(
                                             _SAVE_ADDR_PREFIX, "{}/ensemble_model".format(family_name)))

visual_util.plot_ensemble_weight_median_1d(X=X_valid, weight_sample=ensemble_weights_val,
                                           model_names=ensemble_model_names,
                                           save_addr_prefix=os.path.join(
                                               _SAVE_ADDR_PREFIX, "{}/ensemble_model".format(family_name)))

# model family weights
ensemble_weights_family = np.stack(
    [cond_weights_dict_val[key] for key in family_tree_dict['root']], axis=-1)
visual_util.plot_ensemble_weight_mean_1d(X=X_valid,
                                         weight_sample=ensemble_weights_family,
                                         model_names=family_tree_dict['root'],
                                         save_addr_prefix=os.path.join(
                                             _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))
visual_util.plot_ensemble_weight_median_1d(X=X_valid,
                                           weight_sample=ensemble_weights_family,
                                           model_names=family_tree_dict['root'],
                                           save_addr_prefix=os.path.join(
                                               _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))

""" 2.3.8. visualize: ensemble weight covariance (model compositionality) """
if _PLOT_COMPOSITION:
    for i in range(ensemble_weight_corr.shape[0]):
        corr_mat = ensemble_weight_corr[i]
        x_value = X_valid[i][0]
        visual_util.model_composition_1d(
            x_value, corr_mat, ensemble_weights_val,
            base_pred_dict,
            X_valid, y_valid,
            X_test, y_test,
            model_names=ensemble_model_names,
            save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                   "model composition/{}.png".format(i)))

"""""""""""""""""""""""""""""""""
# 3. Variational Inference
"""""""""""""""""""""""""""""""""
with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_test_pred.pkl'), 'rb') as file:
    base_test_pred = pk.load(file)

with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_valid_pred.pkl'), 'rb') as file:
    base_valid_pred = pk.load(file)

""" 3.1. basic data/algorithm config"""

X_induce = np.expand_dims(np.linspace(np.min(X_test),
                                      np.max(X_test), 10), 1).astype(np.float32)
family_tree_dict = _EXAMPLE_DICTIONARY_SIMPLE

n_inference_sample = 100
n_final_sample = 1000  # number of samples to collect from variational family
max_steps = 20000  # number of training iterations

for family_name in ["mfvi", "sgpr"]:
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
                                         log_ls_weight=DEFAULT_LOG_LS_WEIGHT,
                                         log_ls_resid=DEFAULT_LOG_LS_RESID,
                                         kernel_func=gp.rbf,
                                         ridge_factor=1e-3)

        # assemble kwargs for make_value_setter
        variational_rv_dict = {"ensemble_resid": resid_gp, "sigma": sigma, }
        variational_rv_dict.update(temp_dict)
        variational_rv_dict.update(weight_gp_dict)

        # compute the expected predictive log-likelihood
        with ed.tape() as model_tape:
            with ed.interception(make_value_setter(**variational_rv_dict)):
                y = adaptive_ensemble.model_tailfree(X=X_test, base_pred=base_test_pred, family_tree=family_tree_dict,
                                                     log_ls_weight=DEFAULT_LOG_LS_WEIGHT,
                                                     log_ls_resid=DEFAULT_LOG_LS_RESID, kernel_func=gp.rbf,
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
        optimizer = tf.train.AdamOptimizer(5e-3)
        train_op = optimizer.minimize(-elbo)

        # define init op
        init_op = tf.global_variables_initializer()

        vi_graph.finalize()

    """ 3.3. execute optimization, then sample from variational family """

    with tf.Session(graph=vi_graph) as sess:
        start_time = time.time()

        sess.run(init_op)
        for step in range(max_steps):
            start_time = time.time()
            _, elbo_value, = sess.run([train_op, elbo])
            if step % 500 == 0:
                duration = time.time() - start_time
                print("Step: {:>3d} ELBO: {:.3f}, ({:.3f} sec)".format(
                    step, elbo_value, duration))

        (weight_gp_mean_dict_val, weight_gp_vcov_dict_val,
         resid_gp_mean_val, resid_gp_vcov_val,
         temp_mean_dict_val, temp_sdev_dict_val,
         sigma_mean_val, sigma_sdev_val) = sess.run([
            weight_gp_mean_dict, weight_gp_vcov_dict,
            resid_gp_mean, resid_gp_vcov,
            temp_mean_dict, temp_sdev_dict,  # temperature variational parameters
            sigma_mean, sigma_sdev])

        sess.close()

    with tf.Session() as sess:
        (weight_gp_sample_dict, temp_sample_dict,
         resid_gp_sample, sigma_sample, _, _) = (
            ensemble_variational_family_sample(
                n_final_sample,
                weight_gp_mean_dict_val, weight_gp_vcov_dict_val,
                temp_mean_dict_val, temp_sdev_dict_val,
                resid_gp_mean_val, resid_gp_vcov_val,
                sigma_mean_val, sigma_sdev_val,
                log_ls_weight_mean=DEFAULT_LOG_LS_WEIGHT,
                log_ls_weight_sdev=.01,
                log_ls_resid_mean=DEFAULT_LOG_LS_WEIGHT,
                log_ls_resid_sdev=.01
            ))

        (weight_gp_sample_dict_val, temp_sample_dict_val,
         resid_gp_sample_val, sigma_sample_val) = sess.run([
            weight_gp_sample_dict, temp_sample_dict,
            resid_gp_sample, sigma_sample])

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
                                     ls=np.exp(DEFAULT_LOG_LS_WEIGHT),
                                     kernel_func=gp.rbf).T.astype(np.float32))

    ensemble_resid_valid_sample = (
        gp.sample_posterior_full(X_new=X_valid, X=X_test,
                                 f_sample=resid_gp_sample_val.T,
                                 ls=np.exp(DEFAULT_LOG_LS_RESID),
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
                                                    family_tree=family_tree_dict, weight_gp_dict=raw_weights_dict,
                                                    temp_dict=parent_temp_dict,
                                                    resid_gp_sample=ensemble_resid_valid_sample,
                                                    log_ls_weight=DEFAULT_LOG_LS_WEIGHT))

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

    with open(os.path.join(_SAVE_ADDR_PREFIX,
                           '{}/ensemble_posterior_model_weights_corr.pkl'.format(family_name)), 'rb') as file:
        ensemble_weight_corr = pk.load(file)

    base_pred_dict = {key: value for key, value in base_valid_pred.items()
                      if key in ensemble_model_names}

    visual_util.plot_base_prediction(base_pred=base_pred_dict,
                                     X_valid=X_valid, y_valid=y_valid,
                                     X_train=None, y_train=y_train,
                                     save_addr=os.path.join(
                                         _SAVE_ADDR_PREFIX,
                                         "{}/ensemble_base_model_fit.png".format(family_name)))

    """ 3.5.3. visualize: ensemble posterior predictive mean """

    posterior_mean_mu = np.nanmean(ensemble_mean_val, axis=0)
    posterior_mean_cov = np.nanvar(ensemble_mean_val, axis=0)

    posterior_mean_median = np.nanmedian(ensemble_mean_val, axis=0)
    posterior_mean_quantiles = [
        np.percentile(ensemble_mean_val,
                      [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
        for q in [68, 95, 99]
    ]

    visual_util.gpr_1d_visual(posterior_mean_mu,
                              pred_cov=posterior_mean_cov,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Mean, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_mean.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(posterior_mean_median,
                              pred_cov=None,
                              pred_quantiles=posterior_mean_quantiles,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Mean Quantiles, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_mean_quantile.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                              pred_samples=list(ensemble_mean_val)[:2500],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Samples, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_sample.png".format(family_name))
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

    visual_util.gpr_1d_visual(posterior_resid_mu,
                              pred_cov=posterior_resid_cov,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Residual, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_resid.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(posterior_resid_median,
                              pred_cov=None,
                              pred_quantiles=posterior_resid_quantiles,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Residual Quantiles, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_resid_quantile.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                              pred_samples=list(ensemble_resid_valid_sample)[:2500],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Residual Samples, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_resid_sample.png".format(family_name))
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

    visual_util.gpr_1d_visual(posterior_dist_mu,
                              pred_cov=posterior_dist_cov,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Predictive, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_full.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(posterior_dist_median,
                              pred_cov=None,
                              pred_quantiles=posterior_dist_quantiles,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Predictive Quantiles, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_full_quantile.png".format(family_name))
                              )
    visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                              pred_samples=list(ensemble_sample_val)[:2500],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Predictive Samples, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_full_sample.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(pred_mean=posterior_dist_median,
                              pred_cov=None,
                              pred_quantiles=[
                                  [posterior_dist_median - 3 * np.sqrt(posterior_mean_cov),
                                   posterior_dist_median + 3 * np.sqrt(posterior_mean_cov)],
                                  [posterior_dist_median - 3 * np.sqrt(posterior_dist_cov),
                                   posterior_dist_median + 3 * np.sqrt(posterior_dist_cov)],
                              ],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              quantile_colors=["red", "black"],
                              quantile_alpha=0.2,
                              title="Ensemble Posterior, Uncertainty Decomposition, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_unc_decomp.png".format(family_name))
                              )

    """ 3.5.6. visualize: ensemble posterior reliability """
    y_calib = y_valid[calib_sample_id]
    y_sample_calib = ensemble_sample_val[:, calib_sample_id].T

    visual_util.prob_calibration_1d(
        y_calib, y_sample_calib,
        title="Ensemble, {}".format(family_name_full),
        save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                               "{}/ensemble_calibration_prob.png".format(family_name)))

    """ 3.5.7. visualize: base ensemble weight with uncertainty """
    visual_util.plot_ensemble_weight_mean_1d(X=X_valid, weight_sample=ensemble_weights_val,
                                             model_names=ensemble_model_names,
                                             save_addr_prefix=os.path.join(
                                                 _SAVE_ADDR_PREFIX, "{}/ensemble_model".format(family_name)))

    visual_util.plot_ensemble_weight_median_1d(X=X_valid, weight_sample=ensemble_weights_val,
                                               model_names=ensemble_model_names,
                                               save_addr_prefix=os.path.join(
                                                   _SAVE_ADDR_PREFIX, "{}/ensemble_model".format(family_name)))

    # model family weights
    ensemble_weights_family = np.stack(
        [cond_weights_dict_val[key] for key in family_tree_dict['root']], axis=-1)
    visual_util.plot_ensemble_weight_mean_1d(X=X_valid,
                                             weight_sample=ensemble_weights_family,
                                             model_names=family_tree_dict['root'],
                                             save_addr_prefix=os.path.join(
                                                 _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))
    visual_util.plot_ensemble_weight_median_1d(X=X_valid,
                                               weight_sample=ensemble_weights_family,
                                               model_names=family_tree_dict['root'],
                                               save_addr_prefix=os.path.join(
                                                   _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))

"""""""""""""""""""""""""""""""""
# 4. Augmented VI I: Reliability Index
"""""""""""""""""""""""""""""""""
with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_test_pred.pkl'), 'rb') as file:
    base_test_pred = pk.load(file)

with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_valid_pred.pkl'), 'rb') as file:
    base_valid_pred = pk.load(file)

""" 4.1. basic data/algorithm config"""

X_induce = np.expand_dims(np.linspace(np.min(X_test),
                                      np.max(X_test), 10), 1).astype(np.float32)
family_tree_dict = _EXAMPLE_DICTIONARY_SIMPLE

n_inference_sample = 1000  # number of samples to collect from variational family for approx inference
n_final_sample = 1000  # number of samples to collect from variational family for final summary
max_steps = 20000  # number of training iterations

for family_name in ["mfvi_aug", "sgpr_aug"]:
    if "mfvi" in family_name:
        family_name_full = "Mean-field VI"
        ensemble_variational_family = adaptive_ensemble.variational_mfvi
        ensemble_variational_family_sample = adaptive_ensemble.variational_mfvi_sample
    elif "sgpr" in family_name:
        family_name_full = "Sparse Gaussian Process"
        ensemble_variational_family = adaptive_ensemble.variational_sgpr
        ensemble_variational_family_sample = adaptive_ensemble.variational_sgpr_sample

    """ 4.2. Set up the computational graph """
    vi_graph = tf.Graph()

    with vi_graph.as_default():
        # sample from variational family
        (weight_gp_dict, resid_gp, temp_dict, sigma, _, _,  # variational RVs
         weight_gp_mean_dict, weight_gp_vcov_dict,  # variational parameters, weight GP
         resid_gp_mean, resid_gp_vcov,  # resid GP variational parameters
         temp_mean_dict, temp_sdev_dict,  # temperature variational parameters
         sigma_mean, sigma_sdev  # variational parameters, resid GP
         ) = ensemble_variational_family(X=X_test,
                                         Z=X_induce,
                                         base_pred=base_test_pred,
                                         family_tree=family_tree_dict,
                                         log_ls_weight=DEFAULT_LOG_LS_WEIGHT,
                                         log_ls_resid=DEFAULT_LOG_LS_RESID,
                                         kernel_func=gp.rbf,
                                         ridge_factor=1e-3)

        # assemble kwargs for make_value_setter
        variational_rv_dict = {"ensemble_resid": resid_gp, "sigma": sigma, }
        variational_rv_dict.update(temp_dict)
        variational_rv_dict.update(weight_gp_dict)

        # compute the expected predictive log-likelihood
        with ed.tape() as model_tape:
            with ed.interception(make_value_setter(**variational_rv_dict)):
                y = adaptive_ensemble.model_tailfree(X=X_test, base_pred=base_test_pred, family_tree=family_tree_dict,
                                                     log_ls_weight=DEFAULT_LOG_LS_WEIGHT,
                                                     log_ls_resid=DEFAULT_LOG_LS_RESID, kernel_func=gp.rbf,
                                                     ridge_factor=1e-3)

        log_likelihood = y.distribution.log_prob(y_test)

        # compute the KL divergence
        kl = 0.
        for rv_name, variational_rv in variational_rv_dict.items():
            kl += tf.reduce_sum(
                variational_rv.distribution.kl_divergence(
                    model_tape[rv_name].distribution)
            )

        # compute the calibration score
        y_approx_sample = y.distribution.sample(n_inference_sample)
        calibration_loss = score.make_calibration_loss(
            Y_obs=np.expand_dims(y_test, -1),
            Y_sample=y_approx_sample,
            log_prob=y.distribution.log_prob
        )

        # define loss objective to maximize: ELBO = E_q(-log p(x|z)) - KL(q || p)
        elbo = tf.reduce_mean(log_likelihood - kl)
        loss_op = -elbo + 25 * calibration_loss  # -elbo

        # define optimizer
        optimizer = tf.train.AdamOptimizer(5e-3)
        train_op = optimizer.minimize(loss_op)

        # define init op
        init_op = tf.global_variables_initializer()

        vi_graph.finalize()

    """ 4.3. execute optimization, then sample from variational family """
    # optimize
    with tf.Session(graph=vi_graph) as sess:
        start_time = time.time()

        sess.run(init_op)
        for step in range(max_steps):
            start_time = time.time()
            _, elbo_value, calibration_val = sess.run([
                train_op, elbo, calibration_loss])
            if step % 500 == 0:
                duration = time.time() - start_time
                print("Step: {:>3d} ELBO: {:.3f}, Calibration: {:.3f}, ({:.3f} sec)".format(
                    step, elbo_value, calibration_val, duration))

        (weight_gp_mean_dict_val, weight_gp_vcov_dict_val,
         resid_gp_mean_val, resid_gp_vcov_val,
         temp_mean_dict_val, temp_sdev_dict_val,
         sigma_mean_val, sigma_sdev_val) = sess.run([
            weight_gp_mean_dict, weight_gp_vcov_dict,
            resid_gp_mean, resid_gp_vcov,
            temp_mean_dict, temp_sdev_dict,  # temperature variational parameters
            sigma_mean, sigma_sdev])

        sess.close()

    # sample
    with tf.Session() as sess:
        (weight_gp_sample_dict, temp_sample_dict,
         resid_gp_sample, sigma_sample, _, _) = (
            ensemble_variational_family_sample(
                n_final_sample,
                weight_gp_mean_dict_val, weight_gp_vcov_dict_val,
                temp_mean_dict_val, temp_sdev_dict_val,
                resid_gp_mean_val, resid_gp_vcov_val,
                sigma_mean_val, sigma_sdev_val,
                log_ls_weight_mean=DEFAULT_LOG_LS_WEIGHT,
                log_ls_weight_sdev=.01,
                log_ls_resid_mean=DEFAULT_LOG_LS_WEIGHT,
                log_ls_resid_sdev=.01
            ))

        (weight_gp_sample_dict_val, temp_sample_dict_val,
         resid_gp_sample_val, sigma_sample_val) = sess.run([
            weight_gp_sample_dict, temp_sample_dict,
            resid_gp_sample, sigma_sample])

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

    """ 4.4. prediction and posterior sampling """

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
                                     ls=np.exp(DEFAULT_LOG_LS_WEIGHT),
                                     kernel_func=gp.rbf).T.astype(np.float32))

    ensemble_resid_valid_sample = (
        gp.sample_posterior_full(X_new=X_valid, X=X_test,
                                 f_sample=resid_gp_sample_val.T,
                                 ls=np.exp(DEFAULT_LOG_LS_RESID),
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
                                                    family_tree=family_tree_dict, weight_gp_dict=raw_weights_dict,
                                                    temp_dict=parent_temp_dict,
                                                    resid_gp_sample=ensemble_resid_valid_sample,
                                                    log_ls_weight=DEFAULT_LOG_LS_WEIGHT))

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

    """ 4.4.2. visualize: base prediction """
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

    with open(os.path.join(_SAVE_ADDR_PREFIX,
                           '{}/ensemble_posterior_model_weights_corr.pkl'.format(family_name)), 'rb') as file:
        ensemble_weight_corr = pk.load(file)

    base_pred_dict = {key: value for key, value in base_valid_pred.items()
                      if key in ensemble_model_names}

    visual_util.plot_base_prediction(base_pred=base_pred_dict,
                                     X_valid=X_valid, y_valid=y_valid,
                                     X_train=X_train, y_train=y_train,
                                     save_addr=os.path.join(
                                         _SAVE_ADDR_PREFIX,
                                         "{}/ensemble_base_model_fit.png".format(family_name)))

    """ 4.4.3. visualize: ensemble posterior predictive mean """

    posterior_mean_mu = np.nanmean(ensemble_mean_val, axis=0)
    posterior_mean_cov = np.nanvar(ensemble_mean_val, axis=0)

    posterior_mean_median = np.nanmedian(ensemble_mean_val, axis=0)
    posterior_mean_quantiles = [
        np.percentile(ensemble_mean_val,
                      [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
        for q in [68, 95, 99]
    ]

    visual_util.gpr_1d_visual(posterior_mean_mu,
                              pred_cov=posterior_mean_cov,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Mean, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_mean.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(posterior_mean_median,
                              pred_cov=None,
                              pred_quantiles=posterior_mean_quantiles,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Mean Quantiles, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_mean_quantile.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                              pred_samples=list(ensemble_mean_val)[:2500],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Samples, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_sample.png".format(family_name))
                              )

    """ 4.4.4. visualize: ensemble residual """

    posterior_resid_mu = np.nanmean(ensemble_resid_valid_sample, axis=0)
    posterior_resid_cov = np.nanvar(ensemble_resid_valid_sample, axis=0)

    posterior_resid_median = np.nanmedian(ensemble_resid_valid_sample, axis=0)
    posterior_resid_quantiles = [
        np.percentile(ensemble_resid_valid_sample,
                      [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
        for q in [68, 95, 99]
    ]

    visual_util.gpr_1d_visual(posterior_resid_mu,
                              pred_cov=posterior_resid_cov,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Residual, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_resid.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(posterior_resid_median,
                              pred_cov=None,
                              pred_quantiles=posterior_resid_quantiles,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Residual Quantiles, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_resid_quantile.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                              pred_samples=list(ensemble_resid_valid_sample)[:2500],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Residual Samples, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_resid_sample.png".format(family_name))
                              )

    """ 4.4.5. visualize: ensemble posterior full """

    posterior_dist_mu = np.nanmean(ensemble_sample_val, axis=0)
    posterior_dist_cov = np.nanvar(ensemble_sample_val, axis=0)

    posterior_dist_median = np.nanmedian(ensemble_sample_val, axis=0)
    posterior_dist_quantiles = [
        np.percentile(ensemble_sample_val,
                      [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
        for q in [68, 95, 99]
    ]

    visual_util.gpr_1d_visual(posterior_dist_mu,
                              pred_cov=posterior_dist_cov,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Predictive, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_full.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(posterior_dist_median,
                              pred_cov=None,
                              pred_quantiles=posterior_dist_quantiles,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Predictive Quantiles, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_full_quantile.png".format(family_name))
                              )
    visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                              pred_samples=list(ensemble_sample_val)[:2500],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Predictive Samples, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_full_sample.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(pred_mean=posterior_dist_median,
                              pred_cov=None,
                              pred_quantiles=[
                                  [posterior_dist_median - 3 * np.sqrt(posterior_mean_cov),
                                   posterior_dist_median + 3 * np.sqrt(posterior_mean_cov)],
                                  [posterior_dist_median - 3 * np.sqrt(posterior_dist_cov),
                                   posterior_dist_median + 3 * np.sqrt(posterior_dist_cov)],
                              ],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              quantile_colors=["red", "black"],
                              quantile_alpha=0.2,
                              title="Ensemble Posterior, Uncertainty Decomposition, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_unc_decomp.png".format(family_name))
                              )

    """ 4.4.6. visualize: ensemble posterior reliability """
    y_calib = y_valid[calib_sample_id]
    y_sample_calib = ensemble_sample_val[:, calib_sample_id].T

    visual_util.prob_calibration_1d(
        y_calib, y_sample_calib,
        title="Ensemble, {}".format(family_name_full),
        save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                               "{}/ensemble_calibration_prob.png".format(family_name)))

    """ 4.4.7. visualize: base ensemble weight with uncertainty """
    visual_util.plot_ensemble_weight_mean_1d(X=X_valid, weight_sample=ensemble_weights_val,
                                             model_names=ensemble_model_names,
                                             save_addr_prefix=os.path.join(
                                                 _SAVE_ADDR_PREFIX, "{}/ensemble_model".format(family_name)))

    visual_util.plot_ensemble_weight_median_1d(X=X_valid, weight_sample=ensemble_weights_val,
                                               model_names=ensemble_model_names,
                                               save_addr_prefix=os.path.join(
                                                   _SAVE_ADDR_PREFIX, "{}/ensemble_model".format(family_name)))

    # model family weights
    ensemble_weights_family = np.stack(
        [cond_weights_dict_val[key] for key in family_tree_dict['root']], axis=-1)
    visual_util.plot_ensemble_weight_mean_1d(X=X_valid,
                                             weight_sample=ensemble_weights_family,
                                             model_names=family_tree_dict['root'],
                                             save_addr_prefix=os.path.join(
                                                 _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))
    visual_util.plot_ensemble_weight_median_1d(X=X_valid,
                                               weight_sample=ensemble_weights_family,
                                               model_names=family_tree_dict['root'],
                                               save_addr_prefix=os.path.join(
                                                   _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))

"""""""""""""""""""""""""""""""""
# 5. Augmented VI II: CRPS
"""""""""""""""""""""""""""""""""
with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_test_pred.pkl'), 'rb') as file:
    base_test_pred = pk.load(file)

with open(os.path.join(_SAVE_ADDR_PREFIX, 'base/base_valid_pred.pkl'), 'rb') as file:
    base_valid_pred = pk.load(file)

""" 5.1. basic data/algorithm config"""

X_induce = np.expand_dims(np.linspace(np.min(X_test),
                                      np.max(X_test), 10), 1).astype(np.float32)
family_tree_dict = _EXAMPLE_DICTIONARY_SIMPLE

n_inference_sample = 500  # number of samples to collect from variational family for approx inference
n_final_sample = 10000  # number of samples to collect from variational family for final summary
max_steps = 20000  # number of training iterations

for family_name in ["mfvi_crps", "sgpr_crps"]:
    if "mfvi" in family_name:
        family_name_full = "Mean-field VI"
        ensemble_variational_family = adaptive_ensemble.variational_mfvi
        ensemble_variational_family_sample = adaptive_ensemble.variational_mfvi_sample
    elif "sgpr" in family_name:
        family_name_full = "Sparse Gaussian Process"
        ensemble_variational_family = adaptive_ensemble.variational_sgpr
        ensemble_variational_family_sample = adaptive_ensemble.variational_sgpr_sample

    """ 5.2. Set up the computational graph """
    vi_graph = tf.Graph()

    with vi_graph.as_default():
        # sample from variational family
        (weight_gp_dict, resid_gp, temp_dict, sigma, _, _,  # variational RVs
         weight_gp_mean_dict, weight_gp_vcov_dict,  # variational parameters, weight GP
         resid_gp_mean, resid_gp_vcov,  # resid GP variational parameters
         temp_mean_dict, temp_sdev_dict,  # temperature variational parameters
         sigma_mean, sigma_sdev  # variational parameters, resid GP
         ) = ensemble_variational_family(X=X_test,
                                         Z=X_induce,
                                         base_pred=base_test_pred,
                                         family_tree=family_tree_dict,
                                         log_ls_weight=DEFAULT_LOG_LS_WEIGHT,
                                         log_ls_resid=DEFAULT_LOG_LS_RESID,
                                         kernel_func=gp.rbf,
                                         ridge_factor=1e-3)

        # assemble kwargs for make_value_setter
        variational_rv_dict = {"ensemble_resid": resid_gp, "sigma": sigma, }
        variational_rv_dict.update(temp_dict)
        variational_rv_dict.update(weight_gp_dict)

        # compute the expected predictive log-likelihood
        with ed.tape() as model_tape:
            with ed.interception(make_value_setter(**variational_rv_dict)):
                y = adaptive_ensemble.model_tailfree(X=X_test, base_pred=base_test_pred, family_tree=family_tree_dict,
                                                     log_ls_weight=DEFAULT_LOG_LS_WEIGHT,
                                                     log_ls_resid=DEFAULT_LOG_LS_RESID, kernel_func=gp.rbf,
                                                     ridge_factor=1e-3)

        log_likelihood = y.distribution.log_prob(y_test)

        # compute the KL divergence
        kl = 0.
        for rv_name, variational_rv in variational_rv_dict.items():
            kl += tf.reduce_sum(
                variational_rv.distribution.kl_divergence(
                    model_tape[rv_name].distribution)
            )

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
        elbo = tf.reduce_mean(log_likelihood - kl)
        loss_op = -elbo + 25 * crps_loss  # -elbo

        # define optimizer
        optimizer = tf.train.AdamOptimizer(5e-3)
        train_op = optimizer.minimize(loss_op)

        # define init op
        init_op = tf.global_variables_initializer()

        vi_graph.finalize()

    """ 5.3. execute optimization, then sample from variational family """
    # optimize
    with tf.Session(graph=vi_graph) as sess:
        start_time = time.time()

        sess.run(init_op)
        for step in range(max_steps):
            start_time = time.time()
            _, elbo_value, crps_val = sess.run([
                train_op, elbo, crps_loss])
            if step % 500 == 0:
                duration = time.time() - start_time
                print("Step: {:>3d} ELBO: {:.3f}, Calibration: {:.3f}, ({:.3f} sec)".format(
                    step, elbo_value, crps_val, duration))

        (weight_gp_mean_dict_val, weight_gp_vcov_dict_val,
         resid_gp_mean_val, resid_gp_vcov_val,
         temp_mean_dict_val, temp_sdev_dict_val,
         sigma_mean_val, sigma_sdev_val) = sess.run([
            weight_gp_mean_dict, weight_gp_vcov_dict,
            resid_gp_mean, resid_gp_vcov,
            temp_mean_dict, temp_sdev_dict,  # temperature variational parameters
            sigma_mean, sigma_sdev])

        sess.close()

    # sample
    with tf.Session() as sess:
        (weight_gp_sample_dict, temp_sample_dict,
         resid_gp_sample, sigma_sample, _, _) = (
            ensemble_variational_family_sample(
                n_final_sample,
                weight_gp_mean_dict_val, weight_gp_vcov_dict_val,
                temp_mean_dict_val, temp_sdev_dict_val,
                resid_gp_mean_val, resid_gp_vcov_val,
                sigma_mean_val, sigma_sdev_val,
                log_ls_weight_mean=DEFAULT_LOG_LS_WEIGHT,
                log_ls_weight_sdev=.01,
                log_ls_resid_mean=DEFAULT_LOG_LS_WEIGHT,
                log_ls_resid_sdev=.01
            ))

        (weight_gp_sample_dict_val, temp_sample_dict_val,
         resid_gp_sample_val, sigma_sample_val) = sess.run([
            weight_gp_sample_dict, temp_sample_dict,
            resid_gp_sample, sigma_sample])

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

    """ 5.4. prediction and posterior sampling """

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
                                     ls=np.exp(DEFAULT_LOG_LS_WEIGHT),
                                     kernel_func=gp.rbf).T.astype(np.float32))

    ensemble_resid_valid_sample = (
        gp.sample_posterior_full(X_new=X_valid, X=X_test,
                                 f_sample=resid_gp_sample_val.T,
                                 ls=np.exp(DEFAULT_LOG_LS_RESID),
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
                                                    family_tree=family_tree_dict, weight_gp_dict=raw_weights_dict,
                                                    temp_dict=parent_temp_dict,
                                                    resid_gp_sample=ensemble_resid_valid_sample,
                                                    log_ls_weight=DEFAULT_LOG_LS_WEIGHT))

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

    """ 5.4.2. visualize: base prediction """
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

    with open(os.path.join(_SAVE_ADDR_PREFIX,
                           '{}/ensemble_posterior_model_weights_corr.pkl'.format(family_name)), 'rb') as file:
        ensemble_weight_corr = pk.load(file)

    base_pred_dict = {key: value for key, value in base_valid_pred.items()
                      if key in ensemble_model_names}

    visual_util.plot_base_prediction(base_pred=base_pred_dict,
                                     X_valid=X_valid, y_valid=y_valid,
                                     X_train=X_train, y_train=y_train,
                                     save_addr=os.path.join(
                                         _SAVE_ADDR_PREFIX,
                                         "{}/ensemble_base_model_fit.png".format(family_name)))

    """ 5.4.3. visualize: ensemble posterior predictive mean """

    posterior_mean_mu = np.nanmean(ensemble_mean_val, axis=0)
    posterior_mean_cov = np.nanvar(ensemble_mean_val, axis=0)

    posterior_mean_median = np.nanmedian(ensemble_mean_val, axis=0)
    posterior_mean_quantiles = [
        np.percentile(ensemble_mean_val,
                      [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
        for q in [68, 95, 99]
    ]

    visual_util.gpr_1d_visual(posterior_mean_mu,
                              pred_cov=posterior_mean_cov,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Mean, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_mean.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(posterior_mean_median,
                              pred_cov=None,
                              pred_quantiles=posterior_mean_quantiles,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Mean Quantiles, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_mean_quantile.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                              pred_samples=list(ensemble_mean_val)[:2500],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Samples, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_sample.png".format(family_name))
                              )

    """ 5.4.4. visualize: ensemble residual """

    posterior_resid_mu = np.nanmean(ensemble_resid_valid_sample, axis=0)
    posterior_resid_cov = np.nanvar(ensemble_resid_valid_sample, axis=0)

    posterior_resid_median = np.nanmedian(ensemble_resid_valid_sample, axis=0)
    posterior_resid_quantiles = [
        np.percentile(ensemble_resid_valid_sample,
                      [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
        for q in [68, 95, 99]
    ]

    visual_util.gpr_1d_visual(posterior_resid_mu,
                              pred_cov=posterior_resid_cov,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Residual, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_resid.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(posterior_resid_median,
                              pred_cov=None,
                              pred_quantiles=posterior_resid_quantiles,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Residual Quantiles, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_resid_quantile.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                              pred_samples=list(ensemble_resid_valid_sample)[:2500],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Residual Samples, {}".format(family_name_full),
                              save_addr=os.path.join(
                                  _SAVE_ADDR_PREFIX,
                                  "{}/ensemble_posterior_resid_sample.png".format(family_name))
                              )

    """ 5.4.5. visualize: ensemble posterior full """

    posterior_dist_mu = np.nanmean(ensemble_sample_val, axis=0)
    posterior_dist_cov = np.nanvar(ensemble_sample_val, axis=0)

    posterior_dist_median = np.nanmedian(ensemble_sample_val, axis=0)
    posterior_dist_quantiles = [
        np.percentile(ensemble_sample_val,
                      [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
        for q in [68, 95, 99]
    ]

    visual_util.gpr_1d_visual(posterior_dist_mu,
                              pred_cov=posterior_dist_cov,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Predictive, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_full.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(posterior_dist_median,
                              pred_cov=None,
                              pred_quantiles=posterior_dist_quantiles,
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Predictive Quantiles, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_full_quantile.png".format(family_name))
                              )
    visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                              pred_samples=list(ensemble_sample_val)[:2500],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              title="Ensemble Posterior Predictive Samples, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_full_sample.png".format(family_name))
                              )

    visual_util.gpr_1d_visual(pred_mean=posterior_dist_median,
                              pred_cov=None,
                              pred_quantiles=[
                                  [posterior_dist_median - 3 * np.sqrt(posterior_mean_cov),
                                   posterior_dist_median + 3 * np.sqrt(posterior_mean_cov)],
                                  [posterior_dist_median - 3 * np.sqrt(posterior_dist_cov),
                                   posterior_dist_median + 3 * np.sqrt(posterior_dist_cov)],
                              ],
                              X_train=X_test, y_train=y_test,
                              X_test=X_valid, y_test=y_valid,
                              quantile_colors=["red", "black"],
                              quantile_alpha=0.2,
                              title="Ensemble Posterior, Uncertainty Decomposition, {}".format(family_name_full),
                              save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                     "{}/ensemble_posterior_unc_decomp.png".format(family_name))
                              )

    """ 5.4.6. visualize: ensemble posterior reliability """
    y_calib = y_valid[calib_sample_id]
    y_sample_calib = ensemble_sample_val[:, calib_sample_id].T

    visual_util.prob_calibration_1d(
        y_calib, y_sample_calib,
        title="Ensemble, {}".format(family_name_full),
        save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                               "{}/ensemble_calibration_prob.png".format(family_name)))

    """ 5.4.7. visualize: base ensemble weight with uncertainty """
    visual_util.plot_ensemble_weight_mean_1d(X=X_valid, weight_sample=ensemble_weights_val,
                                             model_names=ensemble_model_names,
                                             save_addr_prefix=os.path.join(
                                                 _SAVE_ADDR_PREFIX, "{}/ensemble_model".format(family_name)))

    visual_util.plot_ensemble_weight_median_1d(X=X_valid, weight_sample=ensemble_weights_val,
                                               model_names=ensemble_model_names,
                                               save_addr_prefix=os.path.join(
                                                   _SAVE_ADDR_PREFIX, "{}/ensemble_model".format(family_name)))

    # model family weights
    ensemble_weights_family = np.stack(
        [cond_weights_dict_val[key] for key in family_tree_dict['root']], axis=-1)
    visual_util.plot_ensemble_weight_mean_1d(X=X_valid,
                                             weight_sample=ensemble_weights_family,
                                             model_names=family_tree_dict['root'],
                                             save_addr_prefix=os.path.join(
                                                 _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))
    visual_util.plot_ensemble_weight_median_1d(X=X_valid,
                                               weight_sample=ensemble_weights_family,
                                               model_names=family_tree_dict['root'],
                                               save_addr_prefix=os.path.join(
                                                   _SAVE_ADDR_PREFIX, "{}/ensemble_family".format(family_name)))

"""""""""""""""""""""""""""""""""
# 6. Nonparametric Calibration I: Isotonic Regression
"""""""""""""""""""""""""""""""""

""" 6.1. prepare hyperparameters"""

# load hyper-parameters
family_name = "sgpr"
family_name_full = {"hmc": "Hamilton MC",
                    "mfvi": "Mean-field VI",
                    "sgpr": "Sparse Gaussian Process"}[family_name]

os.makedirs("{}/{}".format(_SAVE_ADDR_PREFIX, family_name), exist_ok=True)


# load estimates
with open(os.path.join(_SAVE_ADDR_PREFIX,
                       '{}/ensemble_posterior_dist_sample.pkl'.format(family_name)), 'rb') as file:
    ensemble_sample_val = pk.load(file)

""" 6.2. build calibration dataset"""
sample_size = ensemble_sample_val.shape[0]
calib_train_id = np.random.choice(
    calib_sample_id, int(calib_sample_id.size / 2))
calib_test_id = np.asarray(list(set(calib_sample_id) -
                                set(calib_train_id)))

y_calib = y_valid[calib_train_id]
y_calib_sample = ensemble_sample_val[:, calib_train_id].T

calib_data = calib_util.build_calibration_dataset(Y_obs=y_calib,
                                                  Y_sample=y_calib_sample)

with tf.Session() as sess:
    orig_prob, calib_prob = sess.run(
        [calib_data["feature"], calib_data["label"]])

# plt.plot([[0., 0.], [1., 1.]])
# sns.regplot(orig_prob, calib_prob, fit_reg=False)

""" 6.3. fit isotonic regression"""

# fit isotonic regression
ir = IsotonicRegression(y_min=0, y_max=1, out_of_bounds='clip')
ir.fit(orig_prob, calib_prob)

orig_prob_pred = np.linspace(0, 1, num=sample_size)
calib_prob_pred = ir.predict(orig_prob_pred)

# plt.plot([[0., 0.], [1., 1.]])
# sns.regplot(orig_prob, calib_prob,
#             fit_reg=False, scatter_kws={'color': 'green'})
#
# sns.regplot(orig_prob_pred, calib_prob_pred,
#             fit_reg=False, marker='+',
#             scatter_kws={'color': 'red'})
# plt.show()

""" 6.4. produce calibrated posterior sample"""
# specifically, for samples corresponding to each observation,
# produce a empirical cdf in the form of quantiles.

ensemble_sample_calib_val = [
    calib_util.sample_ecdf(n_sample=1000,
                           base_sample=base_sample,
                           quantile=calib_prob_pred) for
    base_sample in ensemble_sample_val.T
]
ensemble_sample_calib_val = np.asarray(ensemble_sample_calib_val).T

# # plot original vs calibrated cdf
# sample_id = 50
# plt.plot(np.sort(ensemble_sample_val[:, sample_id]),
#          orig_prob_pred)
# plt.plot(np.sort(ensemble_sample_val[:, sample_id]),
#          calib_prob_pred)

""" 6.5.1. visualize: ensemble posterior reliability """
os.makedirs(os.path.join(_SAVE_ADDR_PREFIX,
                         "{}/calibration/".format(family_name)),
            exist_ok=True)

posterior_dist_mu = np.nanmean(ensemble_sample_calib_val, axis=0)
posterior_dist_cov = np.nanvar(ensemble_sample_calib_val, axis=0)

posterior_dist_median = np.nanmedian(ensemble_sample_calib_val, axis=0)
posterior_dist_quantiles = [
    np.percentile(ensemble_sample_calib_val,
                  [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
    for q in [68, 95, 99]
]

visual_util.gpr_1d_visual(posterior_dist_mu,
                          pred_cov=posterior_dist_cov,
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Predictive, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/calibration/ensemble_posterior_full_isoreg.png".format(family_name))
                          )

visual_util.gpr_1d_visual(posterior_dist_median,
                          pred_cov=None,
                          pred_quantiles=posterior_dist_quantiles,
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Predictive Quantiles, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/calibration/ensemble_posterior_full_quantile_isoreg.png".format(
                                                     family_name))
                          )
visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                          pred_samples=list(ensemble_sample_calib_val)[:150],
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Predictive Samples, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/calibration/ensemble_posterior_full_sample_isoreg.png".format(
                                                     family_name))
                          )

""" 6.5.2. visualize: ensemble posterior reliability """
y_calib = y_valid[calib_test_id]
y_sample_calib = ensemble_sample_calib_val[:, calib_test_id].T

visual_util.prob_calibration_1d(
    y_calib, y_sample_calib,
    title="Ensemble, Test, {}".format(family_name_full),
    save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                           "{}/calibration/isoreg_calibration_prob_test.png".format(family_name)))

# overall
y_calib = y_valid[calib_sample_id]
y_sample_calib = ensemble_sample_calib_val[:, calib_sample_id].T

visual_util.prob_calibration_1d(
    y_calib, y_sample_calib,
    title="Ensemble, {}".format(family_name_full),
    save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                           "{}/calibration/isoreg_calibration_prob.png".format(family_name)))

"""""""""""""""""""""""""""""""""
# 7. Nonparametric Calibration II: Monotonic GP
"""""""""""""""""""""""""""""""""
# TODO(jereliu): separate ls for main gp and derivative gp
# TODO(jereliu): Urgent: enable sampling for predictive

ADD_CONSTRAINT_POINT = True

""" 7.1. prepare hyperparameters """

# load hyper-parameters
family_name = "sgpr"
family_name_full = {"hmc": "Hamilton MC",
                    "mfvi": "Mean-field VI",
                    "sgpr": "Sparse Gaussian Process"}[family_name]

os.makedirs("{}/{}".format(_SAVE_ADDR_PREFIX, family_name), exist_ok=True)

# load estimates
with open(os.path.join(_SAVE_ADDR_PREFIX,
                       '{}/ensemble_posterior_dist_sample.pkl'.format(family_name)), 'rb') as file:
    ensemble_sample_val = pk.load(file)

""" 7.2. build calibration dataset """
sample_size = ensemble_sample_val.shape[0]
calib_train_id = np.random.choice(
    calib_sample_id, int(calib_sample_id.size / 2))
calib_test_id = np.asarray(list(set(calib_sample_id) -
                                set(calib_train_id)))

y_calib_train = y_valid[calib_train_id]
y_calib_test = y_valid[calib_test_id]
y_calib_sample_train = ensemble_sample_val[:, calib_train_id].T
y_calib_sample_test = ensemble_sample_val[:, calib_test_id].T

calib_data_train = calib_util.build_calibration_dataset(Y_obs=y_calib_train,
                                                        Y_sample=y_calib_sample_train)
calib_data_test = calib_util.build_calibration_dataset(Y_obs=y_calib_test,
                                                       Y_sample=y_calib_sample_test)

with tf.Session() as sess:
    (orig_prob_train, calib_prob_train,
     orig_prob_test, calib_prob_test) = sess.run(
        [calib_data_train["feature"], calib_data_train["label"],
         calib_data_test["feature"], calib_data_test["label"]])

# plt.plot([[0., 0.], [1., 1.]])
# sns.regplot(orig_prob_train, calib_prob_train, fit_reg=False)

if ADD_CONSTRAINT_POINT:
    # add constraint points at [0,0] and [1,1]
    orig_prob_train = np.concatenate([orig_prob_train,
                                      [0., 1.]]).astype(np.float32)
    calib_prob_train = np.concatenate([calib_prob_train,
                                       [0., 1.]]).astype(np.float32)

# prepare data
N = orig_prob_train.size
N_deriv = 100

orig_prob_pred = np.linspace(0, 1, num=sample_size).astype(np.float32)
orig_prob_derv = np.linspace(0, 1, num=N_deriv).astype(np.float32)

orig_prob_train = np.atleast_2d(orig_prob_train).T
orig_prob_derv = np.atleast_2d(orig_prob_derv).T
calib_prob_train = calib_prob_train
orig_prob_pred = np.atleast_2d(orig_prob_pred).T

""" 7.3. fit monotonic GP"""
# default parameters and data
DEFAULT_LS_CALIB_VAL = 0.2
DEFAULT_DERIV_CDF_SCALE = 1e-3

""" 7.3.1. define mcmc computation graph"""
num_results = 5000
num_burnin_steps = 5000

mcmc_graph = tf.Graph()
with mcmc_graph.as_default():
    # build likelihood by explicitly
    target_log_prob_fn = gpr_mono.make_log_likelihood_function(
        X_train=orig_prob_train,
        X_deriv=orig_prob_derv,
        y_train=calib_prob_train,
        ls=DEFAULT_LS_CALIB_VAL,
        deriv_prior_scale=DEFAULT_DERIV_CDF_SCALE,
        ridge_factor=9e-3,
        cdf_constraint=True
    )

    # set up state container
    initial_state = [
        tf.random_normal([N], stddev=0.01, name='init_gp_func'),
        tf.random_normal([N_deriv], stddev=0.01, name='init_gp_derv'),
        tf.constant(0.1, name='init_sigma'),
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
        parallel_iterations=10
    )

    gpf_sample, gpf_deriv_sample, sigma_sample, = state

    # set up init op
    init_op = tf.global_variables_initializer()

    mcmc_graph.finalize()

""" 7.3.2. execute sampling"""
with tf.Session(graph=mcmc_graph) as sess:
    init_op.run()
    [
        f_samples_val,
        f_deriv_samples_val,
        sigma_sample_val,
        is_accepted_,
    ] = sess.run(
        [
            gpf_sample,
            gpf_deriv_sample,
            sigma_sample,
            kernel_results.is_accepted,
        ])
    print('Acceptance Rate: {}'.format(np.mean(is_accepted_)))
    sess.close()

""" 7.3.3. prediction and visualization"""
# prediction
df_pred_val = gp.sample_posterior_full(X_new=orig_prob_pred,
                                       X=orig_prob_derv,
                                       f_sample=f_deriv_samples_val.T,
                                       ls=DEFAULT_LS_CALIB_VAL,
                                       kernel_func=gpr_mono.rbf_hess_1d)
# sample f conditional on f_deriv
calib_prob_pred_val = (
    gpr_mono.sample_posterior_predictive(X_new=orig_prob_pred,
                                         X_obs=orig_prob_train,
                                         X_deriv=orig_prob_derv,
                                         f_sample=f_samples_val.T,
                                         f_deriv_sample=f_deriv_samples_val.T,
                                         kernel_func_ff=gp.rbf,
                                         kernel_func_df=gpr_mono.rbf_grad_1d,
                                         kernel_func_dd=gpr_mono.rbf_hess_1d,
                                         ls=DEFAULT_LS_CALIB_VAL, )
)


# plt.plot([[0., 0.], [1., 1.]])
# sns.regplot(orig_prob_train.squeeze(), calib_prob_train,
#             fit_reg=False, scatter_kws={'color': 'green'})
#
# sns.regplot(orig_prob_pred.squeeze(), np.mean(calib_prob_pred, -1),
#             fit_reg=False, marker='+',
#             scatter_kws={'color': 'red'})
# plt.show()

mu = np.mean(calib_prob_pred_val, axis=1)
mu_deriv = np.mean(df_pred_val, axis=1)
cov = np.var(calib_prob_pred_val, axis=1)
cov_deriv = np.var(df_pred_val, axis=1)

visual_util.gpr_1d_visual(mu, cov,
                          X_train=orig_prob_train,
                          y_train=calib_prob_train,
                          X_test=orig_prob_pred,
                          y_test=None,
                          title="RBF Calibration Fit, Hamilton MC",
                          save_addr=os.path.join(
                              _SAVE_ADDR_PREFIX,
                              "{}/calibration/gpr_calib_fit.png".format(family_name)),
                          y_range=[-0.1, 1.1])

visual_util.gpr_1d_visual(mu_deriv, cov_deriv,
                          X_test=orig_prob_pred,
                          title="RBF Derivative, Hamilton MC",
                          save_addr=os.path.join(
                              _SAVE_ADDR_PREFIX,
                              "{}/calibration/gpr_calib_deriv.png".format(family_name)),
                          add_reference=True, y_range=None)

visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                          pred_samples=list(calib_prob_pred_val.T)[:500],
                          X_train=orig_prob_train,
                          y_train=calib_prob_train,
                          X_test=orig_prob_pred,
                          y_test=None,
                          title="RBF Calibration Samples, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/calibration/gpr_calib_sample.png".format(
                                                     family_name)),
                          y_range=[-0.1, 1.1])

""" 7.4. produce calibrated posterior sample"""
# specifically, for samples corresponding to each observation,
# produce a empirical cdf in the form of quantiles.
calib_prob_pred = np.mean(calib_prob_pred_val, axis=1)
calib_prob_pred[calib_prob_pred > 1.] = 1.
calib_prob_pred[calib_prob_pred < 0.] = 0.

ensemble_sample_calib_val = [
    calib_util.sample_ecdf(n_sample=1000,
                           base_sample=base_sample,
                           quantile=calib_prob_pred) for
    base_sample in ensemble_sample_val.T
]
ensemble_sample_calib_val = np.asarray(ensemble_sample_calib_val).T

# # plot original vs calibrated cdf
# sample_id = 50
# plt.plot(np.sort(ensemble_sample_val[:, sample_id]),
#          orig_prob_pred)
# plt.plot(np.sort(ensemble_sample_val[:, sample_id]),
#          calib_prob_pred)

""" 7.5.1. visualize: ensemble posterior reliability """
os.makedirs(os.path.join(_SAVE_ADDR_PREFIX,
                         "{}/calibration/".format(family_name)),
            exist_ok=True)

posterior_dist_mu = np.nanmean(ensemble_sample_calib_val, axis=0)
posterior_dist_cov = np.nanvar(ensemble_sample_calib_val, axis=0)

posterior_dist_median = np.nanmedian(ensemble_sample_calib_val, axis=0)
posterior_dist_quantiles = [
    np.percentile(ensemble_sample_calib_val,
                  [100 - (100 - q) / 2, (100 - q) / 2], axis=0)
    for q in [68, 95, 99]
]

visual_util.gpr_1d_visual(posterior_dist_mu,
                          pred_cov=posterior_dist_cov,
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Predictive, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/calibration/ensemble_posterior_full_gpr.png".format(family_name))
                          )

visual_util.gpr_1d_visual(posterior_dist_median,
                          pred_cov=None,
                          pred_quantiles=posterior_dist_quantiles,
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Predictive Quantiles, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/calibration/ensemble_posterior_full_quantile_gpr.png".format(
                                                     family_name))
                          )
visual_util.gpr_1d_visual(pred_mean=None, pred_cov=None, pred_quantiles=[],
                          pred_samples=list(ensemble_sample_calib_val)[:150],
                          X_train=X_test, y_train=y_test,
                          X_test=X_valid, y_test=y_valid,
                          title="Ensemble Posterior Predictive Samples, {}".format(family_name_full),
                          save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                                                 "{}/calibration/ensemble_posterior_full_sample_gpr.png".format(
                                                     family_name))
                          )

""" 7.5.2. visualize: ensemble posterior reliability """
y_calib = y_valid[calib_test_id]
y_sample_calib = ensemble_sample_calib_val[:, calib_test_id].T

visual_util.prob_calibration_1d(
    y_calib, y_sample_calib,
    title="Ensemble, Test, {}".format(family_name_full),
    save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                           "{}/calibration/gpr_calibration_prob_test.png".format(family_name)))

# overall
y_calib = y_valid[calib_sample_id]
y_sample_calib = ensemble_sample_calib_val[:, calib_sample_id].T

visual_util.prob_calibration_1d(
    y_calib, y_sample_calib,
    title="Ensemble, {}".format(family_name_full),
    save_addr=os.path.join(_SAVE_ADDR_PREFIX,
                           "{}/calibration/gpr_calibration_prob.png".format(family_name)))

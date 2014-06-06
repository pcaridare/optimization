import matplotlib.pyplot as plt
import logging
import numpy as np
import numpy.linalg as la
import time
from sklearn.cross_validation import KFold

import solvers
import util
from c_extensions.simplex_projection import simplex_projection
from projection import pysimplex_projection
# from projection import pysimplex_projection
import BB, LBFGS, DORE
import config as c
from main import parser

class CrossValidation:

    def __init__(self,k=3,f=None,solver=None,var=None,iter=200):
        self.f=f
        self.solver=solver
        self.var = var
        self.iter = iter
        self.k=k
        self.setup()
        self.kf = KFold(self.n,n_folds=k, indices=True)
        self.iters = [None]*k
        self.times = [None]*k
        self.states = [None]*k

    def save(self):
        pass

    def load(self):
        pass

    def setup(self):
        # load data
        self.A, self.b, self.N, self.block_sizes, x_true=util.load_data(self.f)
        self.NT = self.N.T.tocsr()

        self.n = np.size(self.b)
        self.x_true = np.squeeze(np.array(x_true))

        self.x0 = np.array(util.block_e(self.block_sizes - 1, self.block_sizes))
        logging.debug("Blocks: %s" % self.block_sizes.shape)

        self.options = { 'max_iter': self.iter,
                    'verbose': 0,
                    'suff_dec': 0.003, # FIXME unused
                    'corrections': 500 } # FIXME unused

        self.proj = lambda x: simplex_projection(self.block_sizes - 1,x)
        self.z0 = np.zeros(self.N.shape[1])

    def init_metrics(self):
        self.train_error = []
        self.test_error = []
        self.train_RMSE = []
        self.test_RMSE = []
        self.train_pRMSE = []
        self.test_pRMSE = []

        self.nbins = 6 # emulating class of link by flow
        counts,bins = np.histogram(self.b, bins=self.nbins)
        self.bins = bins
        self.train_bin_error = []
        self.test_bin_error = []
        self.train_bin_RMSE = []
        self.test_bin_RMSE = []
        self.train_bin_pRMSE = []
        self.test_bin_pRMSE = []

    # Run cross-validation and store intermediate states of each run
    def run(self):
        for i,(train,test) in enumerate(self.kf):
            # Setup
            b_train,A_train = self.b[train],self.A[train,:]
            b_test,A_test = self.b[test],self.A[test,:]

            AT = A_train.T.tocsr()

            target = A_train.dot(self.x0) - b_train

            f = lambda z: 0.5 * la.norm(A_train.dot(self.N.dot(z)) + target)**2
            nabla_f = lambda z: self.NT.dot(AT.dot(A_train.dot(self.N.dot(z)) \
                    + target))

            iters, times, states = [], [], []
            def log(iter_,state,duration):
                iters.append(iter_)
                times.append(duration)
                states.append(state)
                start = time.time()
                return start

            # Solve
            logging.debug('[%d] Starting %s solver...' % (i,self.solver))
            if self.solver == 'LBFGS':
                LBFGS.solve(self.z0+1, f, nabla_f, solvers.stopping, log=log,
                        proj=self.proj,options=self.options)
                logging.debug("Took %s time" % str(np.sum(times)))
            elif self.solver == 'BB':
                BB.solve(self.z0,f,nabla_f,solvers.stopping,log=log,
                        proj=self.proj,options=self.options)
            elif self.solver == 'DORE':
                # setup for DORE
                alpha = 0.99
                lsv = util.lsv_operator(A_train, self.N)
                logging.info("Largest singular value: %s" % lsv)
                A_dore = A_train*alpha/lsv
                target_dore = target*alpha/lsv
                DORE.solve(self.z0, lambda z: A_dore.dot(self.N.dot(z)),
                        lambda b: self.N.T.dot(A_dore.T.dot(b)), 
                        target_dore,proj=self.proj,log=log,options=self.options)
                A_dore = None
            logging.debug('[%d] Stopping %s solver...' % (i,self.solver))

            self.iters[i] = iters
            self.times[i] = times
            self.states[i] = states
            AT,A_train,A_test = None,None,None

    # Post process intermediate states of runs
    def post_process(self):
        self.init_metrics()
        self.mean_times = np.mean(np.array([np.cumsum(self.times[i]) for i in \
                range(self.k)]),axis=0)

        def metrics(A,b,X):
            d = X.shape[1]
            diff = A.dot(X) - np.tile(b,(d,1)).T
            error = 0.5 * np.diag(diff.T.dot(diff))
            RMSE = np.sqrt(error/b.size)
            den = np.sum(b)/b.size
            pRMSE = RMSE / den
            return (error, RMSE, pRMSE)

        for i,(train,test) in enumerate(self.kf):
            d = len(self.states[i])
            b_train,A_train = self.b[train],self.A[train,:]
            b_test,A_test = self.b[test],self.A[test,:]
            self.x_hat = self.N.dot(np.array(self.states[i]).T) + \
                    np.tile(self.x0,(d,1)).T

            # Aggregate error
            error, RMSE, pRMSE = metrics(A_train,b_train,self.x_hat)
            self.train_error.append(error)
            self.train_RMSE.append(RMSE)
            self.train_pRMSE.append(pRMSE)
            logging.debug('Train: %8.5e to %8.5e' % (RMSE[0],RMSE[-1]))

            error, RMSE, pRMSE = metrics(A_test,b_test,self.x_hat)
            self.test_error.append(error)
            self.test_RMSE.append(RMSE)
            self.test_pRMSE.append(pRMSE)
            logging.debug('Test:  %8.5e to %8.5e' % (RMSE[0],RMSE[-1]))

            # TODO deprecate
            x_last = self.x_hat[:,-1]
            dist_from_true = np.max(np.abs(x_last-self.x_true))
            start_dist_from_true = np.max(np.abs(self.x_true-self.x0))
            logging.debug('max|x-x_true|: %.2f\nmax|x_init-x_true|: %.2f' \
                    % (dist_from_true, start_dist_from_true))

            # Error metric by link class
            inds = np.digitize(b_train,self.bins)
            indts = np.digitize(b_test,self.bins)
            b_train_error,b_test_error = [],[]
            b_train_RMSE,b_test_RMSE = [],[]
            b_train_pRMSE,b_test_pRMSE = [],[]
            for j in range(1,self.nbins+2):
                ind = inds==j
                indt = indts==j
                if np.all(indt==False) or np.all(ind==False):
                    b_train_error.append(None)
                    b_test_error.append(None)
                    b_train_RMSE.append(None)
                    b_test_RMSE.append(None)
                    b_train_pRMSE.append(None)
                    b_test_pRMSE.append(None)
                    continue

                b_bin,A_bin = b_train[ind],A_train[ind,:]
                b_bint,A_bint = b_test[indt],A_test[indt,:]

                error, RMSE, pRMSE = metrics(A_bin,b_bin,self.x_hat)
                b_train_error.append(error)
                b_train_RMSE.append(RMSE)
                b_train_pRMSE.append(pRMSE)

                error, RMSE, pRMSE = metrics(A_bint,b_bint,self.x_hat)
                b_test_error.append(error)
                b_test_RMSE.append(RMSE)
                b_test_pRMSE.append(pRMSE)

            self.train_bin_error.append(b_train_error)
            self.test_bin_error.append(b_test_error)
            self.train_bin_RMSE.append(b_train_RMSE)
            self.test_bin_RMSE.append(b_test_RMSE)
            self.train_bin_pRMSE.append(b_train_pRMSE)
            self.test_bin_pRMSE.append(b_test_pRMSE)

        # Summary metrics
        self.mean_time = np.mean([np.cumsum(self.times[i])[-1] for i in \
                range(self.k)])
        self.mean_error = np.mean([self.test_error[i][-1] for i in \
                range(self.k)])
        self.mean_RMSE = np.mean([self.test_RMSE[i][-1] for i in \
                range(self.k)])
        logging.debug('mean time: %8.5e, mean error: %8.5e' % (self.mean_time,
                self.mean_error))
        print '\n\n'

    def cleanup(self):
        self.A = None
        self.N = None
        self.NT = None
        self.states = None

    # Plot each of the k tests separately
    def plot_all(self,subplot=None,color='k'):
        if subplot:
            plt.subplot(subplot)

        for i in range(self.k):
            times = np.cumsum(self.times[i])
            if i == 0:
                plt.loglog(times,self.test_RMSE[i],color=color,
                        label='%s-%s (%d iters)' % \
                                (self.solver,self.var,self.iters[0][-1]))
            else:
                plt.loglog(times,self.test_RMSE[i],color=color)
            plt.hold(True)
            plt.loglog(times,self.train_RMSE[i],color=color,alpha=0.25)

        plt.xlabel('CPU time (seconds)')
        plt.ylabel('%d-fold CV RMSE' % self.k)
        plt.title('CV error')
        plt.legend(shadow=True)

    # Plot summary dot for this solver
    def plot(self,subplot=None,color='k'):
        if subplot:
            plt.subplot(subplot)

        plt.plot(self.mean_time,self.mean_RMSE,marker='o',color=color,
                label='%s-%s' % (self.solver,self.var))
        plt.xlabel('Average CPU time (seconds)')
        plt.ylabel('%d-fold CV average RMSE' % self.k)
        plt.title('CV Summary')
        plt.legend(shadow=True,loc='best')

    # Plot bar graph of k tests by link volume bin
    def plot_bar_bins(self,subplot=None,color='k',offset=0,time_max=None,
            metric='RMSE'):
        if subplot:
            plt.subplot(subplot)
        if metric=='RMSE':
            test_metrics = self.test_bin_RMSE
            train_metrics = self.train_bin_RMSE
        elif metric=='pRMSE':
            test_metrics = self.test_bin_pRMSE
            train_metrics = self.train_bin_pRMSE

        ind = len(self.mean_times)-1
        for i in range(len(self.mean_times)-1,-1,-1):
            if self.mean_times[i] <= time_max:
                ind = i
                break

        for j in range(self.nbins+1):
            x = np.array(range(self.nbins+1))
            test_metric = [test_metrics[i][j] for i in \
                    range(self.k) if test_metrics[i][j] != None]
            train_metric = [train_metrics[i][j] for i in \
                    range(self.k) if train_metrics[i][j] != None]
            y1   = np.mean(test_metric,   axis=0)
            y2   = np.mean(train_metric,  axis=0)
            std1 = np.std(test_metric,    axis=0)
            std2 = np.std(train_metric,   axis=0)
            if j == 0:
                plt.bar(x[j]-1+offset,y1[ind],label='%s-%s (%d iters)' % \
                        (self.solver,self.var,self.iters[0][ind]),width=0.15,
                        color=color,yerr=std1[ind])
            else:
                plt.bar(x[j]-1+offset,y1[ind],width=0.15,color=color,
                        yerr=std1[ind])
            plt.hold(True)
            plt.bar(x[j]-1+offset+1./6,y2[ind],width=0.15,color=color,
                    yerr=std2[ind],alpha=0.25)

        xlabels = self.bins
        plt.gca().set_xticklabels(['%8.5e' % x for x in np.hstack((self.bins,
            [np.inf]))])
        plt.xlabel('Link flow volume')
        plt.ylabel('%d-fold CV average %s' % (self.k,metric))
        plt.title('CV %s by link volume (%f sec)' % \
                (metric,time_max))
        plt.legend(shadow=True)

    # Plot each of the k tests separately per link volume bin
    # TODO deprecate
    def plot_bins(self,subplot=None,color='k',time_max=None):
        if subplot:
            plt.subplot(subplot)

        for i in range(self.k):
            times = np.cumsum(self.times[i])
            for j in range(self.nbins+1):
                if self.test_bin_error[i][j] == None or \
                        self.train_bin_error[i][j] == None:
                    continue
                if i == 0:
                    plt.loglog(times,self.test_bin_error[i][j],color=color,
                            label='%s-%s %s' % (self.solver,self.var,
                                self.bins[j]))
                else:
                    plt.loglog(times,self.test_bin_error[i][j],color=color)
                plt.hold(True)
                plt.loglog(times,self.train_bin_error[i][j],color=color,
                        alpha=0.25)

        plt.xlabel('CPU time (seconds)')
        plt.ylabel('%d-fold CV error (L2)' % self.k)
        plt.title('CV error by link volume (%d iterations)' % self.iter)
        plt.legend(shadow=True)


if __name__ == "__main__":
    p = parser()
    args = p.parse_args()
    if args.log in c.ACCEPTED_LOG_LEVELS:
        logging.basicConfig(level=eval('logging.'+args.log))

    m = 200 # multiplier

    cv1 = CrossValidation(k=3,f=args.file,solver='BB',var='z',iter=200*m)
    cv2 = CrossValidation(k=3,f=args.file,solver='DORE',var='z',iter=105*m)
    cv3 = CrossValidation(k=3,f=args.file,solver='LBFGS',var='z',iter=50*m)

    cvs = [cv1,cv2,cv3]
    colors = ['b','m','g']
    for cv in cvs:
        cv.run()
        cv.post_process()
        cv.cleanup()

    # Plot
    [cv.plot(subplot=121,color=c) for (cv,c) in zip(cvs,colors)]
    [cv.plot_all(subplot=122,color=c) for (cv,c) in zip(cvs,colors)]

    # Compute time cap
    time_max = np.min([cv.mean_times[-1] for cv in cvs])

    offsets = [0,1./3,2./3]
    plt.figure()
    [cv.plot_bar_bins(color=c,offset=o,time_max=time_max) for \
            (cv,c,o) in zip(cvs,colors,offsets)]

    plt.figure()
    [cv.plot_bar_bins(color=c,offset=o,time_max=time_max,metric='pRMSE') for \
            (cv,c,o) in zip(cvs,colors,offsets)]

    plt.show()

from __future__ import annotations
import torch
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
sys.path.append(ROOT)
import numpy as np
from trademaster.utils import get_attr, print_metrics
import pandas as pd
from ..custom import Environments
from ..builder import ENVIRONMENTS
from trademaster.pretrained import pretrained
from gym import spaces
from collections import OrderedDict
import pickle
import os.path as osp

@ENVIRONMENTS.register_module()
class PortfolioManagementSARLEnvironment(Environments):
    def __init__(self, config):
        super(PortfolioManagementSARLEnvironment, self).__init__()
        self.dataset = get_attr(config, "dataset", None)
        self.task = get_attr(config, "task", "train")
        self.test_dynamic=int(get_attr(config, "test_dynamic", "-1"))
        self.task_index = int(get_attr(config, "task_index", "-1"))
        self.work_dir = get_attr(config, "work_dir", "")
        length_day = get_attr(self.dataset, "length_day", 10)
        self.day = length_day
        self.df_path = None
        if self.task.startswith("train"):
            self.df_path = get_attr(self.dataset, "train_path", None)
        elif self.task.startswith("valid"):
            self.df_path = get_attr(self.dataset, "valid_path", None)
        else:
            self.df_path = get_attr(self.dataset, "test_path", None)

        self.initial_amount = get_attr(self.dataset, "initial_amount", 100000)
        self.transaction_cost_pct = get_attr(self.dataset, "transaction_cost_pct", 0.001)
        self.tech_indicator_list = get_attr(self.dataset, "tech_indicator_list", [])

        if self.task.startswith("test_dynamic"):
            dynamics_test_path = get_attr(config, "dynamics_test_path", None)
            self.df = pd.read_csv(dynamics_test_path, index_col=0)
            self.start_date=self.df.loc[:,'date'].iloc[0]
            self.end_date = self.df.loc[:,'date'].iloc[-1]
        else:
            self.df = pd.read_csv(self.df_path, index_col=0)
        self.stock_dim = len(self.df.tic.unique())
        self.tic_list = self.df.tic.unique()
        self.state_space_shape = self.stock_dim
        self.action_space_shape = self.stock_dim + 1
        self.length_day = length_day

        ##############################################################
        from trademaster.nets import mLSTMClf
        self.network_dict = torch.load(get_attr(pretrained, "sarl_encoder", None))
        self.net = mLSTMClf(n_features = len(self.tech_indicator_list), layer_num = 1, n_hidden = 128, tic_number = len(self.tic_list)).cuda()
        self.net.load_state_dict(self.network_dict)
        ##############################################################

        self.action_space = spaces.Box(low=-5,
                                       high=5,
                                       shape=(self.action_space_shape,))

        self.observation_space = spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=((len(self.tech_indicator_list) + 1) *
                   self.state_space_shape,))

        self.data = self.df.loc[self.day, :]
        tic_list = list(self.data.tic)
        s_market = np.array([
            self.data[tech].values.tolist()
            for tech in self.tech_indicator_list
        ]).reshape(-1).tolist()
        X = []
        for tic in tic_list:
            df_tic = self.df[self.df.tic == tic]
            df_information = df_tic[self.day - self.length_day:self.day][
                self.tech_indicator_list].to_numpy()
            df_information = torch.from_numpy(
                df_information).float().unsqueeze(0)
            X.append(df_information)
        X = torch.cat(X, dim=0)
        X = X.unsqueeze(0).cuda()
        y = self.net(X)
        y = y.cpu().detach().squeeze().numpy()
        y = y.tolist()
        self.state = np.array(s_market + y)
        self.terminal = False
        self.portfolio_value = self.initial_amount
        self.asset_memory = [self.initial_amount]
        self.portfolio_return_memory = [0]
        self.weights_memory = [[1 / self.action_space_shape] *
                               self.action_space_shape]
        self.date_memory = [self.data.date.unique()[0]]
        self.transaction_cost_memory = []
        self.test_id = 'agent'

    def reset(self):
        self.asset_memory = [self.initial_amount]
        self.day = self.length_day
        self.data = self.df.loc[self.day, :]

        tic_list = list(self.data.tic)
        s_market = np.array([
            self.data[tech].values.tolist()
            for tech in self.tech_indicator_list
        ]).reshape(-1).tolist()
        X = []
        for tic in tic_list:
            df_tic = self.df[self.df.tic == tic]
            df_information = df_tic[self.day - self.length_day:self.day][
                self.tech_indicator_list].to_numpy()
            df_information = torch.from_numpy(
                df_information).float().unsqueeze(0)
            X.append(df_information)
        X = torch.cat(X, dim=0)
        X = X.unsqueeze(0).cuda()
        y = self.net(X)
        y = y.cpu().detach().squeeze().numpy().tolist()
        self.state = np.array(s_market + y)
        self.terminal = False
        self.portfolio_value = self.initial_amount
        self.asset_memory = [self.initial_amount]
        self.portfolio_return_memory = [0]
        self.weights_memory = [[1 / self.action_space_shape] *
                               self.action_space_shape]
        self.date_memory = [self.data.date.unique()[0]]
        self.transaction_cost_memory = []
        return self.state

    def step(self, actions):
        # make judgement about whether our data is running out
        self.terminal = self.day >= len(self.df.index.unique()) - 1
        actions = np.array(actions)

        if self.terminal:
            if self.task.startswith("test_dynamic"):
                print(f'Date from {self.start_date} to {self.end_date}')
            tr, sharpe_ratio, vol, mdd, cr, sor = self.analysis_result()
            stats = OrderedDict(
                {
                    "Total Return": ["{:04f}%".format(tr * 100)],
                    "Sharp Ratio": ["{:04f}".format(sharpe_ratio)],
                    "Volatility": ["{:04f}%".format(vol* 100)],
                    "Max Drawdown": ["{:04f}%".format(mdd* 100)],
                    # "Calmar Ratio": ["{:04f}".format(cr)],
                    # "Sortino Ratio": ["{:04f}".format(sor)],
                }
            )
            table = print_metrics(stats)
            # print(table)

            df_return = self.save_portfolio_return_memory()
            daily_return = df_return.daily_return.values
            df_value = self.save_asset_memory()
            assets = df_value["total assets"].values
            # TODO calculate the buy and hold
            save_dict = OrderedDict(
                {
                    "Profit Margin": tr * 100,
                    "Excess Profit": tr * 100 - 0,
                    "daily_return": daily_return,
                    "total_assets": assets
                }
            )
            metric_save_path = osp.join(self.work_dir,
                                        'metric_' + str(self.task) + '_' + str(self.test_dynamic) + '_' + str(
                                            self.test_id) + '_' + str(self.task_index) + '.pickle')
            if self.task == 'test_dynamic':
                with open(metric_save_path, 'wb') as handle:
                    pickle.dump(save_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)


            return self.state, self.reward, self.terminal, {
                "sharpe_ratio": sharpe_ratio,"total_assets": assets,'table':table
            }
        else:
            # transfer actino into portofolios weights
            weights = self.softmax(actions)
            self.weights_memory.append(weights)
            last_day_memory = self.data

            # step into the next time stamp
            self.day += 1
            self.data = self.df.loc[self.day, :]
            # get the state
            tic_list = list(self.data.tic)
            s_market = np.array([
                self.data[tech].values.tolist()
                for tech in self.tech_indicator_list
            ]).reshape(-1).tolist()
            X = []
            for tic in tic_list:
                df_tic = self.df[self.df.tic == tic]
                df_information = df_tic[self.day - self.length_day:self.day][
                    self.tech_indicator_list].to_numpy()
                df_information = torch.from_numpy(
                    df_information).float().unsqueeze(0)
                X.append(df_information)
            X = torch.cat(X, dim=0)
            X = X.unsqueeze(0).cuda()
            y = self.net(X)
            y = y.cpu().detach().squeeze().numpy().tolist()
            self.state = np.array(s_market + y)

            # get the portfolio return and the new weights(after one day's price variation, the weights will be a little different from
            # the weights when the action is first posed)
            portfolio_weights = weights[1:]
            portfolio_return = sum(
                ((self.data.close.values / last_day_memory.close.values) - 1) *
                portfolio_weights)
            weights_brandnew = self.normalization([weights[0]] + list(
                np.array(weights[1:]) * np.array(
                    (self.data.close.values / last_day_memory.close.values))))
            self.weights_memory.append(weights_brandnew)

            # caculate the transcation fee(there could exist an error of about 0.1% when calculating)
            weights_old = (self.weights_memory[-3])
            weights_new = (self.weights_memory[-2])

            diff_weights = np.sum(
                np.abs(np.array(weights_old) - np.array(weights_new)))
            transcationfee = diff_weights * self.transaction_cost_pct * self.portfolio_value

            # calculate the overal result
            new_portfolio_value = (self.portfolio_value -
                                   transcationfee) * (1 + portfolio_return)
            portfolio_return = (new_portfolio_value -
                                self.portfolio_value) / self.portfolio_value
            self.reward = new_portfolio_value - self.portfolio_value
            self.portfolio_value = new_portfolio_value

            self.portfolio_return_memory.append(portfolio_return)
            self.date_memory.append(self.data.date.unique()[0])
            self.asset_memory.append(new_portfolio_value)
            self.reward = self.reward

        return self.state, self.reward, self.terminal, {"weights_brandnew":weights_brandnew}

    def normalization(self, actions):
        # a normalization function not only for actions to transfer into weights but also for the weights of the
        # portfolios whose prices have been changed through time
        actions = np.array(actions)
        sum = np.sum(actions)
        actions = actions / sum
        return actions

    def softmax(self, actions):
        numerator = np.exp(actions)
        denominator = np.sum(np.exp(actions))
        softmax_output = numerator / denominator
        return softmax_output

    def save_portfolio_return_memory(self):
        # a record of return for each time stamp
        date_list = self.date_memory
        df_date = pd.DataFrame(date_list)
        df_date.columns = ['date']

        return_list = self.portfolio_return_memory
        df_return = pd.DataFrame(return_list)
        df_return.columns = ["daily_return"]
        df_return.index = df_date.date

        return df_return

    def save_asset_memory(self):
        # a record of asset values for each time stamp
        date_list = self.date_memory
        df_date = pd.DataFrame(date_list)
        df_date.columns = ['date']

        assets_list = self.asset_memory
        df_value = pd.DataFrame(assets_list)
        df_value.columns = ["total assets"]
        df_value.index = df_date.date

        return df_value

    def analysis_result(self):
        # A simpler API for the environment to analysis itself when coming to terminal
        df_return = self.save_portfolio_return_memory()
        daily_return = df_return.daily_return.values
        df_value = self.save_asset_memory()
        assets = df_value["total assets"].values
        df = pd.DataFrame()
        df["daily_return"] = daily_return
        df["total assets"] = assets
        return self.evaualte(df)
    def get_daily_return_rate(self,price_list:list):
        return_rate_list=[]
        for i in range(len(price_list)-1):
            return_rate=(price_list[i+1]/price_list[i])-1
            return_rate_list.append(return_rate)
        return return_rate_list
        

    def evaualte(self, df):
        daily_return = df["daily_return"]
        # print(df, df.shape, len(df),len(daily_return))
        neg_ret_lst = df[df["daily_return"] < 0]["daily_return"]
        tr = df["total assets"].values[-1] / (df["total assets"].values[0] + 1e-10) - 1
        return_rate_list=self.get_daily_return_rate(df["total assets"].values)

        sharpe_ratio = np.mean(return_rate_list)*(252)** 0.5 / (np.std(return_rate_list) + 1e-10)
        vol = np.std(return_rate_list)
        mdd = 0
        peak=df["total assets"][0]
        for value in df["total assets"]:
            if value>peak:
                peak=value
            dd=(peak-value)/peak
            if dd>mdd:
                mdd=dd
        cr = np.sum(daily_return) / (mdd + 1e-10)
        sor = np.sum(daily_return) / (np.nan_to_num(np.std(neg_ret_lst),0) + 1e-10) / (np.sqrt(len(daily_return))+1e-10)
        return tr, sharpe_ratio, vol, mdd, cr, sor

#!/usr/bin/env python
# -*- coding: UTF-8 -*-
#######################################################
# This script wraps simple timeseries analysis tools
# and access to the Gnip Search API into a simple tool
# to help the analysis quickly iterate on filters
# a and understand time series trend and events.
#
# If you find this useful or find a bug you don't want
# to fix for yourself, please let me know at @drskippy
#######################################################
__author__="Scott Hendrickson" 

import ConfigParser
import argparse
import calendar
import codecs
import csv
import datetime
import json
import logging
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import os
import pandas as pd
import statsmodels.api as sm
import string
import sys
import time
from functools import partial
from operator import itemgetter
from scipy import signal
from search.results import *

reload(sys)
sys.stdout = codecs.getwriter('utf-8')(sys.stdout)
sys.stdin = codecs.getreader('utf-8')(sys.stdin)

# basic defaults
FROM_PICKLE = True
DEFAULT_CONFIG_FILENAME = os.path.join(".",".gnip")
DATE_FMT = "%Y%m%d%H%M"
DATE_FMT2 = "%Y-%m-%dT%H:%M:%S"
LOG_FILE_PATH = os.path.join(".","time_series.log")

# set up simple logging
logging.basicConfig(filename=LOG_FILE_PATH,level=logging.DEBUG)
logging.info("#"*70)
logging.info("################# started {} #################".format(datetime.datetime.now()))

# tunable defaults
CHAR_UPPER_CUTOFF = 20          # don't include tokens longer than CHAR_UPPER_CUTOFF
TWEET_SAMPLE = 4000             # tweets to collect for peak topics
MIN_SNR = 0.75                  # signal to noise threshold for peak detection
MAX_N_PEAKS = 5                 # maximum number of peaks to output
MAX_PEAK_WIDTH = 36             # max peak width in periods
MIN_PEAK_WIDTH = 2              # min peak width in periods
START_OFFSET = 3                # start searching peak start, max and end this many periods before peak detection algo's index
N_MOVING = 4                    # average over buckets
OUTLIER_FRAC = 0.8              # cut off values over 80% above or below the average
PLOTS_PREFIX = os.path.join(".","plots")
PLOT_DELTA_Y = 1.2              # spacing of y values in dotplot

logging.debug("CHAR_UPPER_CUTOFF={},TWEET_SAMPLE={},MIN_SNR={},MAX_N_PEAKS={},MAX_PEAK_WIDTH={},MIN_PEAK_WIDTH={},START_OFFSET={},N_MOVING={},OUTLIER_FRAC={},PLOTS_PREFIX={},PLOT_DELTA_Y={}".format(
    CHAR_UPPER_CUTOFF 
    , TWEET_SAMPLE 
    , MIN_SNR 
    , MAX_N_PEAKS 
    , MAX_PEAK_WIDTH 
    , MIN_PEAK_WIDTH 
    , START_OFFSET 
    , N_MOVING 
    , OUTLIER_FRAC 
    , PLOTS_PREFIX 
    , PLOT_DELTA_Y ))


class TimeSeries():
    """Containter class for data collected from the API and associated analysis outputs"""
    pass

class GnipSearchTimeseries():

    def __init__(self, token_list_size=40):
        """Retrieve and analysis timesseries and associated interesting trends, spikes and tweet content."""
        # default tokenizer and character limit
        char_upper_cutoff = CHAR_UPPER_CUTOFF
        self.token_list_size = int(token_list_size)
        #############################################
        # CONFIG FILE/COMMAND LINE OPTIONS PATTERN
        # parse config file
        config_from_file = self.config_file()
        # set required fields to None.  Sequence of setting is:
        #  (1) config file
        #  (2) command line
        # if still none, then fail
        self.user = None
        self.password = None
        self.stream_url = None
        if config_from_file is not None:
            try:
                # command line options take presidence if they exist
                self.user = config_from_file.get('creds', 'un')
                self.password = config_from_file.get('creds', 'pwd')
                self.stream_url = config_from_file.get('endpoint', 'url')
            except (ConfigParser.NoOptionError,
                    ConfigParser.NoSectionError) as e:
                logging.warn("Error reading configuration file ({}), ignoring configuration file.".format(e))
        # parse the command line options
        self.options = self.args().parse_args()
        # set up the job
        # over ride config file with command line args if present
        if self.options.user is not None:
            self.user = self.options.user
        if self.options.password is not None:
            self.password = self.options.password
        if self.options.stream_url is not None:
            self.stream_url = self.options.stream_url
        
        # search v2 uses a different url
        if "data-api.twitter.com" not in self.stream_url:
            logging.error("gnipSearch timeline tools require Search V2. Exiting.")
            exit(-1)

        # set some options that should not be changed for this anaysis
        self.options.paged = True
        self.options.search_v2 = True
        self.options.max = 500
        self.options.query = False
        
        # log the attributes of this class including all of the options
        for v in dir(self):
            # except don't log the password!
            if not v.startswith('__') and not callable(getattr(self,v)) and not v.startswith('password'):
                logging.debug(" {} = {}".format(v, getattr(self,v)))

    def config_file(self):
        """Search for a valid config file in the standard locations."""
        config = ConfigParser.ConfigParser()
        # (1) default file name precidence
        config.read(DEFAULT_CONFIG_FILENAME)
        logging.info("attempting to read config file {}".format(DEFAULT_CONFIG_FILENAME))
        if not config.has_section("creds"):
            # (2) environment variable file name second
            if 'GNIP_CONFIG_FILE' in os.environ:
                config_filename = os.environ['GNIP_CONFIG_FILE']
                logging.info("attempting to read config file {}".format(config_filename))
                config.read(config_filename)
        if config.has_section("creds") and config.has_section("endpoint"):
            return config
        else:
            logging.warn("no creds or endpoint section found in config file, attempting to proceed without config info from file")
            return None

    def args(self):
        "Set up the command line argments and the associated help strings."""
        twitter_parser = argparse.ArgumentParser(
                description="GnipSearch timeline tools")
        twitter_parser.add_argument("-b", "--bucket", dest="count_bucket", 
                default="day", 
                help="Bucket size for counts query. Options are day, hour, minute (default is 'day').")
        twitter_parser.add_argument("-e", "--end-date", dest="end", 
                default=None,
                help="End of datetime window, format 'YYYY-mm-DDTHH:MM' (default: most recent activities)")
        twitter_parser.add_argument("-f", "--filter", dest="filter", 
                default="from:jrmontag OR from:gnip",
                help="PowerTrack filter rule (See: http://support.gnip.com/customer/portal/articles/901152-powertrack-operators)")
        twitter_parser.add_argument("-l", "--stream-url", dest="stream_url", 
                default=None,
                help="Url of search endpoint. (See your Gnip console.)")
        twitter_parser.add_argument("-p", "--password", dest="password", default=None, 
                help="Password")
        twitter_parser.add_argument("-s", "--start-date", dest="start", 
                default=None,
                help="Start of datetime window, format 'YYYY-mm-DDTHH:MM' (default: 30 days ago)")
        twitter_parser.add_argument("-u", "--user-name", dest="user", 
                default=None,
                help="User name")
        twitter_parser.add_argument("-t", "--get-topics", dest="get_topics", action="store_true", 
                default=False,
                help="Set flag to evaluate peak topics (this may take a few minutes)")
        twitter_parser.add_argument("-w", "--output-file-path", dest="output_file_path", 
                default=None,
                help="Create files in ./OUTPUT-FILE-PATH. This path must exists and will not be created. This options is available only with -a option. Default is no output files.")
        return twitter_parser
    
    def get_results(self):
        """Execute API calls to the timeseries data and tweet data we need for analysis. Perform analysis
        as we go because we often need results for next steps."""
        ######################
        # (1) Get the timeline
        ######################
        logging.info("retrieving timeline counts")
        results_timeseries = Results( self.user
            , self.password
            , self.stream_url
            , self.options.paged
            , self.options.output_file_path
            , pt_filter=self.options.filter
            , max_results=int(self.options.max)
            , start=self.options.start
            , end=self.options.end
            , count_bucket=self.options.count_bucket
            , show_query=self.options.query
            , search_v2=self.options.search_v2
            )
        # sort by date
        res_timeseries = sorted(results_timeseries.get_time_series(), key = itemgetter(0))
        # calculate total time interval span
        time_min_date = min(res_timeseries, key = itemgetter(2))[2]
        time_max_date = max(res_timeseries, key = itemgetter(2))[2]
        time_min = float(calendar.timegm(time_min_date.timetuple()))
        time_max = float(calendar.timegm(time_max_date.timetuple()))
        time_span = time_max - time_min
        logging.debug("time_min = {}, time_max = {}, time_span = {}".format(time_min, time_max, time_span))
        # create a simple object to hold our data 
        ts = TimeSeries()
        ts.dates = []
        ts.x = []
        ts.counts = []
        # load and format data
        for i in res_timeseries:
            ts.dates.append(i[2])
            ts.counts.append(float(i[1]))
            # crate a independent variable in interval [0.0,1.0]
            ts.x.append((calendar.timegm(datetime.datetime.strptime(i[0], DATE_FMT).timetuple()) - time_min)/time_span)
        logging.info("read {} time items from search API".format(len(ts.dates)))
        if len(ts.dates) < 35:
            logging.warn("peak detection with with fewer than ~35 points is unreliable!")
        logging.debug('dates: ' + ','.join(map(str, ts.dates[:10])) + "...")
        logging.debug('counts: ' + ','.join(map(str, ts.counts[:10])) + "...")
        logging.debug('indep var: ' + ','.join(map(str, ts.x[:10])) + "...")
        ######################
        # (2) Detrend and remove prominent period
        ######################
        logging.info("detrending timeline counts")
        no_trend = signal.detrend(np.array(ts.counts))
        # determine period of data
        df = (ts.dates[1] - ts.dates[0]).total_seconds()
        if df == 86400:
            # day counts, average over week
            n_buckets = 7
            n_avgs = {i:[] for i in range(n_buckets)}
            for t,c in zip(ts.dates, no_trend):
                n_avgs[t.weekday()].append(c)
        elif df == 3600:
            # hour counts, average over day
            n_buckets = 24
            n_avgs = {i:[] for i in range(n_buckets)}
            for t,c in zip(ts.dates, no_trend):
                n_avgs[t.hour].append(c)
        elif df == 60:
            # minute counts; average over day
            n_buckets = 24*60
            n_avgs = {i:[] for i in range(n_buckets)}
            for t,c in zip(ts.dates, no_trend):
                n_avgs[t.minute].append(c)
        else:
            sys.stderr.write("Weird interval problem! Exiting.\n")
            sys.exit()
        logging.info("averaging over periods of {} buckets".format(n_buckets))
        # remove upper outliers from averages 
        df_avg_all = {i:np.average(n_avgs[i]) for i in range(n_buckets)}
        logging.debug("bucket averages: {}".format(','.join(map(str, [df_avg_all[i] for i in df_avg_all]))))
        n_avgs_remove_outliers = {i: [j for j in n_avgs[i] 
            if  abs(j - df_avg_all[i])/df_avg_all[i] < (1. + OUTLIER_FRAC) ]
            for i in range(n_buckets)}
        df_avg = {i:np.average(n_avgs_remove_outliers[i]) for i in range(n_buckets)}
        logging.debug("bucket averages w/o outliers: {}".format(','.join(map(str, [df_avg[i] for i in df_avg]))))

        # flatten cycle
        ts.counts_no_cycle_trend = np.array([no_trend[i] - df_avg[ts.dates[i].hour] for i in range(len(ts.counts))])
        logging.debug('no trend: ' + ','.join(map(str, ts.counts_no_cycle_trend[:10])) + "...")

        ######################
        # (3) Moving average 
        ######################
        ts.moving = np.convolve(ts.counts, np.ones((N_MOVING,))/N_MOVING, mode='valid')
        logging.debug('moving ({}): '.format(N_MOVING) + ','.join(map(str, ts.moving[:10])) + "...")

        ######################
        # (4) Peak detection
        ######################
        peakind = signal.find_peaks_cwt(ts.counts_no_cycle_trend, np.arange(MIN_PEAK_WIDTH,MAX_PEAK_WIDTH), min_snr = MIN_SNR)
        n_peaks = min([MAX_N_PEAKS, len(peakind)])
        logging.debug('peaks ({}): '.format(n_peaks) + ','.join(map(str, peakind)))
        logging.debug('peaks ({}): '.format(n_peaks) + ','.join(map(str, [ts.dates[i] for i in peakind])))
        
        # top peaks determined by peak volume, better way?
        # peak detector algorithm:
        #      * is leading by a few periods or up to 1 period late
        #      * finds peaks up to MAX_PEAK_WIDTH wide
        #
        #   algorithm for geting peak start, peak and end parameters:
        #      find max, find fwhm, 
        #      find start, step past peak, keep track of volume and peak height, 
        #      stop at end of period or when timeseries turns upward
    
        peaks = []
        for i in peakind:
            # find the first max in the possible window
            # starting at i-1 is empiricle determination
            tmp = [0]
            i_max = None
            for j in range(max([i-1, 0]), min(i + MAX_PEAK_WIDTH, len(ts.dates))):
                if tmp[-1] > ts.counts[j]:
                    # record index of peak
                    i_max = j - 1 
                    break
                else:
                    tmp.append(ts.counts[j])
            # if i_max is not set, we dropped off the loop without a decrease in timeseries
            if i_max is None:
                i_max = min(i + MAX_PEAK_WIDTH, len(ts.dates) - 1)
                logging.warn("Didn't find a proper peak! Trying to continue.")
            p_max = max(tmp)
            h_max = p_max/2.
            p_sum = 0
            tmp = []
            i_start, i_finish = None, None
            # start far enough back to find valid raising point on front of peak
            for j in range(max([i - START_OFFSET, 0]), min(i + MAX_PEAK_WIDTH, len(ts.dates))): 
                if j <= i_max:
                    # before or on peak
                    if ts.counts[j] > h_max:
                        if i_start is None:
                            i_start = j - 1
                        # keep track of counts for volume
                        tmp.append(ts.counts[j])
                else:
                    # past peak and going up
                    if ts.counts[j] > tmp[-1]:
                        # timeseries is increasing, use the last point to get the minimum
                        i_finish = j - 1
                        break
                    elif ts.counts[j] < h_max:
                        # dropped below half max
                        i_finish = j
                        break
                    else:
                        # keep track of counts for volume
                        tmp.append(ts.counts[j])
            if i_start is None:
                # if i_start is not set, we didn't satisfy the conditions above
                i_start = max([i - START_OFFSET, 0])
                logging.ward("Didn't find a proper peak start point. Trying to continue.")
            if i_finish is None:
                i_finish = min(i + MAX_PEAK_WIDTH, len(ts.dates)) - 1
                logging.ward("Didn't find a proper peak end point. Trying to continue.")
            p_volume = sum(tmp)
            peaks.append([ i , p_volume , (i, i_start, i_max, i_finish
                                            , h_max  , p_max, p_volume
                                            , ts.dates[i_start], ts.dates[i_max], ts.dates[i_finish])])
        # top n_peaks by volume
        top_peaks = sorted(peaks, key = itemgetter(1))[-n_peaks:]
        # re-sort peaks by date
        ts.top_peaks = sorted(top_peaks, key = itemgetter(0))
        logging.debug('top peaks ({}): '.format(len(ts.top_peaks)) + ','.join(map(str, ts.top_peaks[:4])) + "...")
    
        ######################
        # (5) high/low frequency 
        ######################
        ts.cycle, ts.trend = sm.tsa.filters.hpfilter(np.array(ts.counts))
        logging.debug('cycle: ' + ','.join(map(str, ts.cycle[:10])) + "...")
        logging.debug('trend: ' + ','.join(map(str, ts.trend[:10])) + "...")
    
        ######################
        # (6) n-grams for top peaks
        ######################
        ts.topics = []
        if self.options.get_topics:
            logging.info("retrieving tweets for peak topics")
            for a in ts.top_peaks:
                # start at peak
                ds = datetime.datetime.strftime(a[2][8], DATE_FMT2)
                # estimate how long to get TWEET_SAMPLE tweets
                # a[1] is max tweets per period
                est_periods = float(TWEET_SAMPLE)/a[1]
                # df comes from above, in seconds
                # time resolution is hours
                est_time = max([int(est_periods * df), 60])
                logging.debug("est_periods={}, est_time={}".format(est_periods, est_time))
                #
                if a[2][8] + datetime.timedelta(seconds=est_time) < a[2][9]:
                    de = datetime.datetime.strftime(a[2][8] + datetime.timedelta(seconds=est_time), DATE_FMT2)
                elif a[2][8] < a[2][9]:
                    de = datetime.datetime.strftime(a[2][9], DATE_FMT2)
                else:
                    de = datetime.datetime.strftime(a[2][8] + datetime.timedelta(seconds=60), DATE_FMT2)
                logging.info("retreive data for peak index={} in date range [{},{}]".format(a[0], ds, de))
                res = Results(
                    self.user
                    , self.password
                    , self.stream_url
                    , self.options.paged
                    , self.options.output_file_path
                    , pt_filter=self.options.filter
                    , max_results=int(self.options.max)
                    , start=ds
                    , end=de
                    , count_bucket=None
                    , show_query=self.options.query
                    , search_v2=self.options.search_v2
                    , hard_max = TWEET_SAMPLE
                    )
                logging.info("retrieved {} records".format(len(res)))
                n_grams_counts = list(res.get_top_grams(n=self.token_list_size))
                ts.topics.append(n_grams_counts)
                logging.debug('n_grams for peak index={}: '.format(a[0]) + ','.join(map(str, [i[4] for i in n_grams][:10])) + "...")
        return ts

    def dotplot(self, x, labels, path = "dotplot.png"):
        """Makeshift dotplots in matplotlib. This is not completely general and encodes labels and
        parameter selections that are particular to n-gram dotplots."""
        logging.info("dotplot called, writing image to path={}".format(path))
        # split n_gram_counts into 2 data sets
        n = len(labels)/2
        x1, x2 = x[:n], x[n:]
        labels1, labels2 = labels[:n], labels[n:]
        # create enough equally spaced y values for the horizontal lines
        ys = [r*PLOT_DELTA_Y for r in range(1,len(labels2)+1)]
        # give ourselves a little extra room on the plot
        maxx = max(x)*1.05
        maxy = max(ys)*1.05
        # set up plots to be a factor taller than the default size
        # make factor proportional to the number of n-grams plotted
        size = plt.gcf().get_size_inches()
        # factor of n/10 is empirical
        scale_denom = 10
        fig, (ax1, ax2) = plt.subplots(nrows=2, ncols=1,figsize=(size[0], size[1]*n/scale_denom))
        logging.debug("plotting top {} terms".format(n))
        logging.debug("plot size=({},{})".format(size[0], size[1]*n/scale_denom))
        #  first plot 1-grams
        ax1.set_xlim(0,maxx)
        ax1.set_ylim(0,maxy)
        ticks = ax1.yaxis.set_ticks(ys)
        text = ax1.yaxis.set_ticklabels(labels1)
        for ct, item in enumerate(labels1):
            ax1.hlines(ys[ct], 0, maxx, linestyle='dashed', color='0.9')
        ax1.plot(x1, ys, 'ko')
        ax1.set_title("1-grams")
        # second plot 2-grams
        ax2.set_xlim(0,maxx)
        ax2.set_ylim(0,maxy)
        ticks = ax2.yaxis.set_ticks(ys)
        text = ax2.yaxis.set_ticklabels(labels2)
        for ct, item in enumerate(labels2):
            ax2.hlines(ys[ct], 0, maxx, linestyle='dashed', color='0.9')
        ax2.plot(x2, ys, 'ko')
        ax2.set_title("2-grams")
        ax2.set_xlabel("Fraction of Mentions")
        #
        plt.tight_layout()
        plt.savefig(path)
        plt.close("all")

    def plots(self, ts, out_type="png"):
        """Basic choice for plotting analysis. If you wish to extend this class, over-
        write this method."""
        # creat a valid file name, in this case and additional requirement is no spaces
        valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
        filter_prefix_name = ''.join(c for c in self.options.filter if c in valid_chars)
        filter_prefix_name = filter_prefix_name.replace(" ", "_")
        if len(filter_prefix_name) > 16:
            filter_prefix_name = filter_prefix_name[:16]
        ######################
        # timeline
        ######################
        df0 = pd.Series(ts.counts, index=ts.dates)
        df0.plot()
        plt.ylabel("Counts")
        plt.title(filter_prefix_name)
        plt.tight_layout()
        plt.savefig(os.path.join(PLOTS_PREFIX, '{}_{}.{}'.format(filter_prefix_name, "time_line", out_type)))
        plt.close("all")
        ######################
        # cycle and trend
        ######################
        df1 = pd.DataFrame({"cycle":ts.cycle, "trend":ts.trend}, index=ts.dates)
        df1.plot()
        plt.ylabel("Counts")
        plt.title(filter_prefix_name)
        plt.tight_layout()
        plt.savefig(os.path.join(PLOTS_PREFIX, '{}_{}.{}'.format(filter_prefix_name, "cycle_trend_line", out_type)))
        plt.close("all")
        ######################
        # moving avg
        ######################
        df2 = pd.DataFrame({"moving":ts.moving}, index=ts.dates[:len(ts.moving)])
        df2.plot()
        plt.ylabel("Counts")
        plt.title(filter_prefix_name)
        plt.tight_layout()
        plt.savefig(os.path.join(PLOTS_PREFIX, '{}_{}.{}'.format(filter_prefix_name, "mov_avg_line", out_type)))
        plt.close("all")
        ######################
        # timeline with peaks marked by vertical bands
        ######################
        df3 = pd.Series(ts.counts, index=ts.dates)
        df3.plot()
        # peaks
        for a in ts.top_peaks:
            xs = a[2][7]
            xp = a[2][8]
            xe = a[2][9]
            y = a[2][5]
            # need to get x and y locs
            plt.axvspan(xs, xe, ymin=0, ymax = y, linewidth=1, color='g', alpha=0.2)
            plt.axvline(xp, ymin=0, ymax = y, linewidth=1, color='y')
        plt.ylabel("Counts")
        plt.title(filter_prefix_name)
        plt.tight_layout()
        plt.savefig(PLOTS_PREFIX + '/time_peaks_line.png')
        plt.close("all")
        ######################
        # n-grams to help determine topics of peaks
        ######################
        for n, p in enumerate(ts.topics):
            x = []
            labels = []
            for i in p:
                x.append(i[1])
                labels.append(i[4])
            self.dotplot(x, labels, os.path.join(PLOTS_PREFIX, "{}_{}_{}.{}".format(filter_prefix_name, "peak", n, out_type)))

if __name__ == "__main__":
    """ Simple command line utility."""
    import pickle
    g = GnipSearchTimeseries()
    if FROM_PICKLE:
        ts = pickle.load(open("./time_series.pickle", "rb"))
    else:
        ts = g.get_results()
        pickle.dump(ts,open("./time_series.pickle", "wb"))
    g.plots(ts)
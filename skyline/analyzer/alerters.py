from __future__ import division
import logging
from smtplib import SMTP
import alerters
try:
    import urllib2
except ImportError:
    import urllib.request
    import urllib.error
from requests.utils import quote

# Added for graphs showing Redis data
import traceback
import redis
from msgpack import Unpacker
import datetime as dt

# @modified 20160820 - Issue #23 Test dependency updates
# Use Agg for matplotlib==1.5.2 upgrade, backwards compatibile
import matplotlib
matplotlib.use('Agg')
# @modified 20161228 - Feature #1828: ionosphere - mirage Redis data features
# Handle flake8 E402
if True:
    import matplotlib.pyplot as plt
    from matplotlib.pylab import rcParams
    from matplotlib.dates import DateFormatter
    # import io
    import numpy as np
    import pandas as pd
    import syslog

    import os.path
    import sys
    import resource

sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir))
sys.path.insert(0, os.path.dirname(__file__))

python_version = int(sys.version_info[0])
if python_version == 2:
    from email.MIMEMultipart import MIMEMultipart
    from email.MIMEText import MIMEText
    from email.MIMEImage import MIMEImage
if python_version == 3:
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.image import MIMEImage

# @modified 20161228 - Feature #1828: ionosphere - mirage Redis data features
# Handle flake8 E402
if True:
    import settings
    import skyline_version

skyline_app = 'analyzer'
skyline_app_logger = '%sLog' % skyline_app
logger = logging.getLogger(skyline_app_logger)
skyline_app_logfile = '%s/%s.log' % (settings.LOG_PATH, skyline_app)

skyline_version = skyline_version.__absolute_version__

"""
Create any alerter you want here. The function will be invoked from
trigger_alert.

Two arguments will be passed, both of them tuples: alert and metric.

alert: the tuple specified in your settings:\n
    alert[0]: The matched substring of the anomalous metric\n
    alert[1]: the name of the strategy being used to alert\n
    alert[2]: The timeout of the alert that was triggered\n
metric: information about the anomaly itself\n
    metric[0]: the anomalous value\n
    metric[1]: The full name of the anomalous metric\n
    metric[2]: anomaly timestamp\n

"""

# FULL_DURATION to hours so that Analyzer surfaces the relevant timeseries data
# in the graph
try:
    full_duration_seconds = int(settings.FULL_DURATION)
except:
    full_duration_seconds = 86400
full_duration_in_hours = full_duration_seconds / 60 / 60


def alert_smtp(alert, metric):
    """
    Called by :func:`~trigger_alert` and sends an alert via smtp to the
    recipients that are configured for the metric.

    """
    LOCAL_DEBUG = False
    logger = logging.getLogger(skyline_app_logger)
    if settings.ENABLE_DEBUG or LOCAL_DEBUG:
        logger.info('debug :: alert_smtp - sending smtp alert')
        logger.info('debug :: alert_smtp - Memory usage at start: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    # FULL_DURATION to hours so that analyzer surfaces the relevant timeseries data
    # in the graph
    full_duration_in_hours = int(settings.FULL_DURATION) / 3600

    # For backwards compatibility
    if '@' in alert[1]:
        sender = settings.ALERT_SENDER
        recipient = alert[1]
    else:
        sender = settings.SMTP_OPTS['sender']
        # @modified 20160806 - Added default_recipient
        try:
            recipients = settings.SMTP_OPTS['recipients'][alert[0]]
            use_default_recipient = False
        except:
            use_default_recipient = True
        if use_default_recipient:
            try:
                recipients = settings.SMTP_OPTS['default_recipient']
                logger.info(
                    'alert_smtp - using default_recipient as no recipients are configured for %s' %
                    str(alert[0]))
            except:
                logger.error(
                    'error :: alert_smtp - no known recipient for %s' %
                    str(alert[0]))
                return False

    # Backwards compatibility
    if type(recipients) is str:
        recipients = [recipients]

    base_name = str(metric[1])

    unencoded_graph_title = 'Skyline Analyzer - ALERT at %s hours %s - %s' % (full_duration_in_hours, metric[1], metric[0])
    if settings.ENABLE_DEBUG or LOCAL_DEBUG:
        logger.info('debug :: alert_smtp - unencoded_graph_title: %s' % unencoded_graph_title)
    graph_title_string = quote(unencoded_graph_title, safe='')
    graph_title = '&title=%s' % graph_title_string

    if settings.GRAPHITE_PORT != '':
        link = '%s://%s:%s/render/?from=-%shours&target=cactiStyle(%s)%s%s&colorList=orange' % (
            settings.GRAPHITE_PROTOCOL, settings.GRAPHITE_HOST,
            settings.GRAPHITE_PORT, str(int(full_duration_in_hours)), metric[1],
            settings.GRAPHITE_GRAPH_SETTINGS, graph_title)
    else:
        link = '%s://%s/render/?from=-%shours&target=cactiStyle(%s)%s%s&colorList=orange' % (
            settings.GRAPHITE_PROTOCOL, settings.GRAPHITE_HOST,
            str(int(full_duration_in_hours)), metric[1], settings.GRAPHITE_GRAPH_SETTINGS,
            graph_title)

    content_id = metric[1]
    image_data = None
    if settings.SMTP_OPTS.get('embed-images'):
        try:
            image_data = urllib2.urlopen(link).read()
            if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - image data OK')
        except urllib2.URLError:
            logger.error(traceback.format_exc())
            logger.error('error :: alert_smtp - failed to get image graph')
            logger.error('error :: alert_smtp - %s' % str(link))
            image_data = None
            if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - image data None')

    if LOCAL_DEBUG:
        logger.info('debug :: alert_smtp - Memory usage after image_data: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    # If we failed to get the image or if it was explicitly disabled,
    # use the image URL instead of the content.
    if image_data is None:
        img_tag = '<img src="%s"/>' % link
    else:
        img_tag = '<img src="cid:%s"/>' % content_id
        if settings.ENABLE_DEBUG or LOCAL_DEBUG:
            logger.info('debug :: alert_smtp - img_tag: %s' % img_tag)

    redis_image_data = None
    try:
        plot_redis_data = settings.PLOT_REDIS_DATA
    except:
        plot_redis_data = False

    if settings.SMTP_OPTS.get('embed-images') and plot_redis_data:
        # Create graph from Redis data
        try:
            REDIS_ALERTER_CONN = redis.StrictRedis(unix_socket_path=settings.REDIS_SOCKET_PATH)
        except:
            logger.error('error :: alert_smtp - redis connection failed')

        redis_metric_key = '%s%s' % (settings.FULL_NAMESPACE, metric[1])
        try:
            raw_series = REDIS_ALERTER_CONN.get(redis_metric_key)
            if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - raw_series: %s' % 'OK')
        except:
            if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - raw_series: %s' % 'FAIL')

        try:
            if LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - Memory usage before get Redis timeseries data: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            unpacker = Unpacker(use_list=True)
            unpacker.feed(raw_series)
            timeseries_x = [float(item[0]) for item in unpacker]
            unpacker = Unpacker(use_list=True)
            unpacker.feed(raw_series)
            timeseries_y = [item[1] for item in unpacker]

            unpacker = Unpacker(use_list=False)
            unpacker.feed(raw_series)
            timeseries = list(unpacker)
            if LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - Memory usage after get Redis timeseries data: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        except:
            logger.error('error :: alert_smtp - unpack timeseries failed')
            timeseries = None

        pd_series_values = None
        if timeseries:
            try:
                if LOCAL_DEBUG:
                    logger.info('debug :: alert_smtp - Memory usage before pd.Series: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                values = pd.Series([x[1] for x in timeseries])
                # Because the truth value of a Series is ambiguous
                pd_series_values = True
                if LOCAL_DEBUG:
                    logger.info('debug :: alert_smtp - Memory usage after pd.Series: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            except:
                logger.error('error :: alert_smtp - pandas value series on timeseries failed')

        if pd_series_values:
            try:
                array_median = np.median(values)
                if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                    logger.info('debug :: alert_smtp - values median: %s' % str(array_median))

                array_amax = np.amax(values)
                if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                    logger.info('debug :: alert_smtp - array_amax: %s' % str(array_amax))
                array_amin = np.amin(values)
                if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                    logger.info('debug :: alert_smtp - array_amin: %s' % str(array_amin))
                mean = values.mean()
                if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                    logger.info('debug :: alert_smtp - mean: %s' % str(mean))
                stdDev = values.std()
                if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                    logger.info('debug :: alert_smtp - stdDev: %s' % str(stdDev))

                sigma3 = 3 * stdDev
                if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                    logger.info('debug :: alert_smtp - sigma3: %s' % str(sigma3))

                # sigma3_series = [sigma3] * len(values)

                sigma3_upper_bound = mean + sigma3
                try:
                    sigma3_lower_bound = mean - sigma3
                except:
                    sigma3_lower_bound = 0

                sigma3_upper_series = [sigma3_upper_bound] * len(values)
                sigma3_lower_series = [sigma3_lower_bound] * len(values)
                amax_series = [array_amax] * len(values)
                amin_series = [array_amin] * len(values)
                mean_series = [mean] * len(values)
            except:
                logger.error('error :: alert_smtp - numpy ops on series failed')
                mean_series = None

        if mean_series:
            graph_title = 'Skyline Analyzer - ALERT - at %s hours - Redis data\n%s - anomalous value: %s' % (full_duration_in_hours, metric[1], metric[0])
            # @modified 20160814 - Bug #1558: Memory leak in Analyzer
            # I think the buf is causing a memory leak, trying a file
            # if python_version == 3:
            #     buf = io.StringIO()
            # else:
            #     buf = io.BytesIO()
            buf = '%s/%s.%s.%s.png' % (
                settings.SKYLINE_TMP_DIR, skyline_app, str(metric[0]), metric[0])

            if LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - Memory usage before plot Redis data: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

            # Too big
            # rcParams['figure.figsize'] = 12, 6
            rcParams['figure.figsize'] = 8, 4
            try:
                # fig = plt.figure()
                fig = plt.figure(frameon=False)
                ax = fig.add_subplot(111)
                ax.set_title(graph_title, fontsize='small')
                ax.set_axis_bgcolor('black')
                try:
                    datetimes = [dt.datetime.utcfromtimestamp(ts) for ts in timeseries_x]
                    if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                        logger.info('debug :: alert_smtp - datetimes: %s' % 'OK')
                except:
                    logger.error('error :: alert_smtp - datetimes: %s' % 'FAIL')

                plt.xticks(rotation=0, horizontalalignment='center')
                xfmt = DateFormatter('%a %H:%M')
                plt.gca().xaxis.set_major_formatter(xfmt)

                ax.xaxis.set_major_formatter(xfmt)

                ax.plot(datetimes, timeseries_y, color='orange', lw=0.6, zorder=3)
                ax.tick_params(axis='both', labelsize='xx-small')

                max_value_label = 'max - %s' % str(array_amax)
                ax.plot(datetimes, amax_series, lw=1, label=max_value_label, color='m', ls='--', zorder=4)
                min_value_label = 'min - %s' % str(array_amin)
                ax.plot(datetimes, amin_series, lw=1, label=min_value_label, color='b', ls='--', zorder=4)
                mean_value_label = 'mean - %s' % str(mean)
                ax.plot(datetimes, mean_series, lw=1.5, label=mean_value_label, color='g', ls='--', zorder=4)

                sigma3_text = (r'3$\sigma$')
                # sigma3_label = '%s - %s' % (str(sigma3_text), str(sigma3))

                sigma3_upper_label = '%s upper - %s' % (str(sigma3_text), str(sigma3_upper_bound))
                ax.plot(datetimes, sigma3_upper_series, lw=1, label=sigma3_upper_label, color='r', ls='solid', zorder=4)

                if sigma3_lower_bound > 0:
                    sigma3_lower_label = '%s lower - %s' % (str(sigma3_text), str(sigma3_lower_bound))
                    ax.plot(datetimes, sigma3_lower_series, lw=1, label=sigma3_lower_label, color='r', ls='solid', zorder=4)

                ax.get_yaxis().get_major_formatter().set_useOffset(False)
                ax.get_yaxis().get_major_formatter().set_scientific(False)

                # Shrink current axis's height by 10% on the bottom
                box = ax.get_position()
                ax.set_position([box.x0, box.y0 + box.height * 0.1,
                                 box.width, box.height * 0.9])

                # Put a legend below current axis
                ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.05),
                          fancybox=True, shadow=True, ncol=4, fontsize='x-small')
                plt.rc('lines', lw=2, color='w')

                plt.grid(True)

                ax.grid(b=True, which='both', axis='both', color='lightgray',
                        linestyle='solid', alpha=0.5, linewidth=0.6)
                ax.set_axis_bgcolor('black')

                rcParams['xtick.direction'] = 'out'
                rcParams['ytick.direction'] = 'out'
                ax.margins(y=.02, x=.03)
                # tight_layout removes the legend box
                # fig.tight_layout()
                try:
                    if LOCAL_DEBUG:
                        logger.info('debug :: alert_smtp - Memory usage before plt.savefig: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                    plt.savefig(buf, format='png')

                    if settings.IONOSPHERE_ENABLED:
                        timeseries_dir = base_name.replace('.', '/')

                        training_data_dir = '%s/%s/' % (
                            settings.IONOSPHERE_DATA_FOLDER, str(metric[2]),
                            timeseries_dir)
                        if os.path.exists(training_data_dir):
                            training_data_redis_image = '%s/%s.png' % (
                                training_data_dir, base_name)
                            try:
                                plt.savefig(training_data_redis_image, format='png')
                                logger.info(
                                    'alert_smtp - save redis training data image - %s' % (
                                        training_data_redis_image))
                            except:
                                logger.info(traceback.format_exc())
                                logger.error(
                                    'error :: alert_smtp - could not save - %s' % (
                                        training_data_redis_image))
                        else:
                            logger.error(
                                'error :: alert_smtp - path does not exist, could not save - %s' % (
                                    training_data_redis_image))

                    # @added 20160814 - Bug #1558: Memory leak in Analyzer
                    # As per http://www.mail-archive.com/matplotlib-users@lists.sourceforge.net/msg13222.html
                    # savefig in the parent process was causing the memory leak
                    # the below fig.clf() and plt.close() did not resolve this
                    # however spawing a multiprocessing process for alert_smtp
                    # does solve this as issue as all memory is freed when the
                    # process terminates.
                    fig.clf()
                    plt.close(fig)
                    redis_graph_content_id = 'redis.%s' % metric[1]
                    redis_image_data = True
                    if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                        logger.info('debug :: alert_smtp - savefig: %s' % 'OK')
                        logger.info('debug :: alert_smtp - Memory usage after plt.savefig: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                except:
                    logger.error('error :: alert_smtp - plt.savefig: %s' % 'FAIL')
            except:
                logger.error('error :: alert_smtp - could not build plot')
                logger.info(traceback.format_exc())

    if LOCAL_DEBUG:
        logger.info('debug :: alert_smtp - Memory usage before email: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    if redis_image_data:
        redis_img_tag = '<img src="cid:%s"/>' % redis_graph_content_id
        if settings.ENABLE_DEBUG or LOCAL_DEBUG:
            logger.info('debug :: alert_smtp - redis_img_tag: %s' % str(redis_img_tag))
    else:
        redis_img_tag = '<img src="none"/>'

    try:
        body = '<h3><font color="#dd3023">Sky</font><font color="#6698FF">line</font><font color="black"> Analyzer alert</font></h3><br>'
        body += '<font color="black">metric: <b>%s</b></font><br>' % metric[1]
        body += '<font color="black">Anomalous value: %s</font><br>' % str(metric[0])
        body += '<font color="black">Anomaly timestamp: %s</font><br>' % str(metric[2])
        body += '<font color="black">At hours: %s</font><br>' % str(full_duration_in_hours)
        body += '<font color="black">Next alert in: %s seconds</font><br>' % str(alert[2])
        if settings.IONOSPHERE_ENABLED:
            body += '<h3><font color="#dd3023">Ionosphere :: </font><font color="#6698FF">training data</font><font color="black"></font></h3>'
            ionosphere_link = '%s/ionosphere?timestamp=%s&metric=%s' % (
                settings.SKYLINE_URL, str(metric[2]), str(metric[1]))
            body += '<font color="black">To use this timeseries to train Skyline that this is not anomalous manage this training data at:<br>'
            body += '<a href="%s">%s</a></font>' % (ionosphere_link, ionosphere_link)
        if redis_image_data:
            body += '<font color="black">min: %s  | max: %s   | mean: %s <br>' % (
                str(array_amin), str(array_amax), str(mean))
            body += '3-sigma: %s <br>' % str(sigma3)
            body += '3-sigma upper bound: %s   | 3-sigma lower bound: %s <br></font>' % (
                str(sigma3_upper_bound), str(sigma3_lower_bound))
            body += '<h3><font color="black">Redis data at FULL_DURATION</font></h3><br>'
            body += '<div dir="ltr">:%s<br></div>' % redis_img_tag
        if image_data:
            body += '<h3><font color="black">Graphite data at FULL_DURATION (may be aggregated)</font></h3>'
            body += '<div dir="ltr"><a href="%s">%s</a><br></div><br>' % (link, img_tag)
            body += '<font color="black">Clicking on the above graph will open to the Graphite graph with current data</font><br>'
        if redis_image_data:
            body += '<font color="black">To disable the Redis data graph view, set PLOT_REDIS_DATA to False in your settings.py, if the Graphite graph is sufficient for you,<br>'
            body += 'however do note that will remove the 3-sigma and mean value too.</font>'
        body += '<br>'
        body += '<div dir="ltr" align="right"><font color="#dd3023">Sky</font><font color="#6698FF">line</font><font color="black"> version :: %s</font></div><br>' % str(skyline_version)
    except:
        logger.error('error :: alert_smtp - could not build body')
        logger.info(traceback.format_exc())

    for recipient in recipients:
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = '[Skyline alert] - Analyzer ALERT - ' + metric[1]
            msg['From'] = sender
            msg['To'] = recipient

            msg.attach(MIMEText(body, 'html'))

            if redis_image_data:
                try:
                    # @modified 20160814 - Bug #1558: Memory leak in Analyzer
                    # I think the buf is causing a memory leak, trying a file
                    # buf.seek(0)
                    # msg_plot_attachment = MIMEImage(buf.read())
                    # msg_plot_attachment = MIMEImage(buf.read())
                    try:
                        with open(buf, 'r') as f:
                            plot_image_data = f.read()
                        try:
                            os.remove(buf)
                        except OSError:
                            logger.error(
                                'error :: alert_smtp - failed to remove file - %s' % buf)
                            logger.info(traceback.format_exc())
                            pass
                    except:
                        logger.error('error :: failed to read plot file - %s' % buf)
                        plot_image_data = None

                    # @added 20161124 - Branch #922: ionosphere
                    msg_plot_attachment = MIMEImage(plot_image_data)
                    msg_plot_attachment.add_header('Content-ID', '<%s>' % redis_graph_content_id)
                    msg.attach(msg_plot_attachment)
                    if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                        logger.info('debug :: alert_smtp - msg_plot_attachment - redis data done')
                except:
                    logger.error('error :: alert_smtp - msg_plot_attachment')
                    logger.info(traceback.format_exc())

            if image_data is not None:
                try:
                    msg_attachment = MIMEImage(image_data)
                    msg_attachment.add_header('Content-ID', '<%s>' % content_id)
                    msg.attach(msg_attachment)
                    if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                        logger.info('debug :: alert_smtp - msg_attachment - Graphite img source done')
                except:
                    logger.error('error :: alert_smtp - msg_attachment')
                    logger.info(traceback.format_exc())
        except:
            logger.error('error :: alert_smtp - could not attach')
            logger.info(traceback.format_exc())

        s = SMTP('127.0.0.1')
        try:
            s.sendmail(sender, recipient, msg.as_string())
            if settings.ENABLE_DEBUG or LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - message sent to %s OK' % str(recipient))
        except:
            logger.error('error :: alert_smtp - could not send email to %s' % str(recipient))
            logger.info(traceback.format_exc())

        s.quit()

        if LOCAL_DEBUG:
            logger.info('debug :: alert_smtp - Memory usage after email: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

        if redis_image_data:
            # buf.seek(0)
            # buf.write('none')
            if LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - Memory usage before del redis_image_data objects: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            del raw_series
            del unpacker
            del timeseries[:]
            del timeseries_x[:]
            del timeseries_y[:]
            del values
            del datetimes[:]
            del msg_plot_attachment
            del redis_image_data
            # We del all variables that are floats as they become unique objects and
            # can result in what appears to be a memory leak, but is not, it is
            # just the way Python handles floats
            del mean
            del array_amin
            del array_amax
            del stdDev
            del sigma3
            if LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - Memory usage after del redis_image_data objects: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            if LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - Memory usage before del fig object: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            # @added 20160814 - Bug #1558: Memory leak in Analyzer
            #                   Issue #21 Memory leak in Analyzer - https://github.com/earthgecko/skyline/issues/21
            # As per http://www.mail-archive.com/matplotlib-users@lists.sourceforge.net/msg13222.html
            fig.clf()
            plt.close(fig)
            del fig
            if LOCAL_DEBUG:
                logger.info('debug :: alert_smtp - Memory usage after del fig object: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

        if LOCAL_DEBUG:
            logger.info('debug :: alert_smtp - Memory usage before del other objects: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        del recipients[:]
        del body
        del msg
        del image_data
        del msg_attachment
        if LOCAL_DEBUG:
            logger.info('debug :: alert_smtp - Memory usage after del other objects: %s (kb)' % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return


def alert_pagerduty(alert, metric):
    """
    Called by :func:`~trigger_alert` and sends an alert via PagerDuty
    """
    if settings.PAGERDUTY_ENABLED:
        import pygerduty
        pager = pygerduty.PagerDuty(settings.PAGERDUTY_OPTS['subdomain'], settings.PAGERDUTY_OPTS['auth_token'])
        pager.trigger_incident(settings.PAGERDUTY_OPTS['key'], "Anomalous metric: %s (value: %s)" % (metric[1], metric[0]))
    else:
        return


def alert_hipchat(alert, metric):
    """
    Called by :func:`~trigger_alert` and sends an alert the hipchat room that is
    configured in settings.py.
    """

    if settings.HIPCHAT_ENABLED:
        sender = settings.HIPCHAT_OPTS['sender']
        import hipchat
        hipster = hipchat.HipChat(token=settings.HIPCHAT_OPTS['auth_token'])
        rooms = settings.HIPCHAT_OPTS['rooms'][alert[0]]

        unencoded_graph_title = 'Skyline Analyzer - ALERT at %s hours %s - %s' % (full_duration_in_hours, metric[1], metric[0])
        graph_title_string = quote(unencoded_graph_title, safe='')
        graph_title = '&title=%s' % graph_title_string

        if settings.GRAPHITE_PORT != '':
            link = '%s://%s:%s/render/?from=-%shours&target=cactiStyle(%s)%s%s&colorList=orange' % (
                settings.GRAPHITE_PROTOCOL, settings.GRAPHITE_HOST,
                settings.GRAPHITE_PORT, full_duration_in_hours, metric[1],
                settings.GRAPHITE_GRAPH_SETTINGS, graph_title)
        else:
            link = '%s://%s/render/?from=-%shours&target=cactiStyle(%s)%s%s&colorList=orange' % (
                settings.GRAPHITE_PROTOCOL, settings.GRAPHITE_HOST,
                full_duration_in_hours, metric[1],
                settings.GRAPHITE_GRAPH_SETTINGS, graph_title)
        embed_graph = "<a href='" + link + "'><img height='308' src='" + link + "'>" + metric[1] + "</a>"

        for room in rooms:
            message = '%s - Analyzer - anomalous metric: %s (value: %s) at %s hours %s' % (
                sender, metric[1], metric[0], full_duration_in_hours, embed_graph)
            hipchat_color = settings.HIPCHAT_OPTS['color']
            hipster.method(
                'rooms/message', method='POST',
                parameters={'room_id': room, 'from': 'Skyline', 'color': hipchat_color, 'message': message})
    else:
        return


def alert_syslog(alert, metric):
    """
    Called by :func:`~trigger_alert` and log anomalies to syslog.

    """
    if settings.SYSLOG_ENABLED:
        syslog_ident = settings.SYSLOG_OPTS['ident']
        message = str('Analyzer - Anomalous metric: %s (value: %s)' % (metric[1], metric[0]))
        if sys.version_info[:2] == (2, 6):
            syslog.openlog(syslog_ident, syslog.LOG_PID, syslog.LOG_LOCAL4)
        elif sys.version_info[:2] == (2, 7):
            syslog.openlog(ident="skyline", logoption=syslog.LOG_PID, facility=syslog.LOG_LOCAL4)
        elif sys.version_info[:1] == (3):
            syslog.openlog(ident="skyline", logoption=syslog.LOG_PID, facility=syslog.LOG_LOCAL4)
        else:
            syslog.openlog(syslog_ident, syslog.LOG_PID, syslog.LOG_LOCAL4)
        syslog.syslog(4, message)
    else:
        return


def trigger_alert(alert, metric):
    """
    Called by :py:func:`skyline.analyzer.Analyzer.spawn_alerter_process
    <analyzer.analyzer.Analyzer.spawn_alerter_process>` to trigger an alert.

    Analyzer passes two arguments, both of them tuples. The alerting strategy
    is determined and the approriate alert def is then called and passed the
    tuples.

    :param alert:
        The alert tuple specified in settings.py.\n
        alert[0]: The matched substring of the anomalous metric\n
        alert[1]: the name of the strategy being used to alert\n
        alert[2]: The timeout of the alert that was triggered\n
    :param meric:
        The metric tuple.\n
        metric[0]: the anomalous value\n
        metric[1]: The full name of the anomalous metric\n
        metric[2]: anomaly timestamp\n

    """

    if '@' in alert[1]:
        strategy = 'alert_smtp'
    else:
        strategy = 'alert_%s' % alert[1]

    try:
        getattr(alerters, strategy)(alert, metric)
    except:
        logger.error('error :: alerters - %s - getattr error' % strategy)
        logger.info(traceback.format_exc())

#!/usr/bin/env python

"""Modern Honeypot Network - Client version.

Usage:
    mhnclient.py -c <config_path> [-D]

Options:
    -c <config_path>                Path to config file to use.
    -D                              Daemonize flag.
"""
import os
import json
import time
import pickle
import logging
from os import path, makedirs
from threading import Timer
from itertools import groupby
from datetime import datetime

import requests
import pyparsing as pyp
from docopt import docopt
from sqlalchemy import (
        create_engine, String, Integer, Column,
        Float)
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


logger = logging.getLogger('mhnclient')


class MHNClient(object):

    def __init__(self, **kwargs):
        self.api_url = kwargs.get('api_url')
        self.sensor_uuid = kwargs.get('sensor_uuid')
        self.snort_rules = kwargs.get('snort_rules')
        self.session = requests.Session()
        self.session.auth = (self.sensor_uuid, self.sensor_uuid)
        self.session.headers = {'Content-Type': 'application/json'}
        self.download_time = kwargs.get('download_time')
        self.rules_timer = Timer(1, self._checktime)
        self.rules_timer.start()

        def on_response(r, *args, **kwargs):
            if r.status_code != 200:
                logger.info("Bad HTTP status code: {} for '{}'".format(
                    r.status_code, r.url))
        self.session.hooks = dict(response=on_response)

        def on_new_attacks(new_alerts):
            logger.info("Detected {} new alerts. Posting to '{}'.".format(
                    len(new_alerts), self.attack_url))
            for al in new_alerts:
                self.post_attack(al)
        self.alerter = AlertEventHandler(
                self.sensor_uuid, kwargs.get('alert_file'),
                kwargs.get('dionaea_db'), on_new_attacks)

    @staticmethod
    def _safe_request(request, *args, **kwargs):
        try:
            return request(*args, **kwargs)
        except Exception as e:
            logger.error('Caught exception:\n{}'.format(e))

    @staticmethod
    def _read_resp(resp):
        if not resp:
            return {}
        try:
            return resp.json()
        except ValueError:
            logger.info("Bad response format (No JSON) for {}:\n{}".format(
                     resp.url, resp.text))
            return {}

    def run(self):
        self.connect_sensor()
        self.download_rules()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.cleanup()

    def post(self, *args, **kwargs):
        return self._safe_request(self.session.post, *args, **kwargs)

    def get(self, *args, **kwargs):
        return self._safe_request(self.session.get, *args, **kwargs)

    def _checktime(self):
        timenow  = datetime.now().strftime('%H:%M')
        if timenow == self.download_time:
            self.download_rules()
        self.rules_timer = Timer(1, self._checktime)
        self.rules_timer.start()

    def cleanup(self):
        self.rules_timer.cancel()
        if self.alerter.observer.isAlive():
            self.alerter.observer.stop()
            self.alerter.observer.join()

    def connect_sensor(self):
        connresp = self.post(self.connect_url)
        jsresp = self._read_resp(connresp)
        if jsresp:
            logger.info("Started Honeypot '{}' on {}.".format(
                    jsresp.get('hostname'), connresp.json().get('ip')))
            self.alerter.observer.start()
        else:
            # Try connecting again in 10.0 seconds.
            self.conn_timer = Timer(10.0, self.connect_sensor)
            self.conn_timer.start()

    def download_rules(self):
        logger.info("Downloading rules from '{}'.".format(self.rule_url))
        rules = self.get(self.rule_url, stream=True)
        if  not rules:
            return
        if not rules.status_code == 200:
            return
        rulespath = path.join(self.snort_rules, 'mhn.rules')
        with open(rulespath, 'wb') as f:
            for chunk in rules.iter_content(chunk_size=1024):
                if chunk: # filter out keep-alive new chunks
                    f.write(chunk)
                    f.flush()
        os.system('killall -SIGHUP snort')

    def post_attack(self, alert):
        self.post(self.attack_url, data=alert.to_json())

    @property
    def attack_url(self):
        return '{}/attack/'.format(self.api_url)

    @property
    def connect_url(self):
        return '{}/sensor/{}/connect/'.format(self.api_url,
                                              self.sensor_uuid)

    @property
    def rule_url(self):
        server_url = path.dirname(self.api_url)
        return '{}/static/mhn.rules'.format(server_url)


# SQLAlchemy's built-in declarative base class.
Base = declarative_base()


class Connection(Base):
    """
    Represents a Dionaea connection entry.
    """

    __tablename__ = 'connections'
    connection = Column(Integer, primary_key=True)
    connection_type = Column(String(15))
    connection_protocol = Column(String(15))
    connection_timestamp = Column(Float())
    connection_root = Column(Integer)
    local_host = Column(String(15))
    local_port = Column(String(6))
    remote_host = Column(String(15))
    remote_hostname = Column(String(20))
    remote_port = Column(String(6))

    def to_dict(self):
        local_host = Connection.clean_ipv4(self.local_host)
        remote_host = Connection.clean_ipv4(self.remote_host)
        return dict(connection=self.connection,
                    connection_type=self.connection_type,
                    connection_protocol=self.connection_protocol,
                    connection_timestamp=self.connection_timestamp,
                    connection_root=self.connection_root,
                    local_host=local_host, local_port=self.local_port,
                    remote_host=remote_host, remote_port=self.remote_port,
                    remote_hostname=self.remote_hostname)

    @property
    def datetime(self):
        return datetime.utcfromtimestamp(self.connection_timestamp)

    def to_json(self):
        date = self.datetime
        _dict = self.to_dict()
        _dict['date'] = date.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        return json.dumps(_dict)

    def is_alert(self, alert):
        """
        Compares a connection database entry from dionaea with an
        alert log from snort, and determines whether they are the
        same event.
        A snort alert and a dionaea connection are the same iff:
        * Attacker's ip address is equal
        * Target's ip address is equal
        * Target port is the same
        * Logged within the same second.
        """
        if alert.destination_ip == self.local_host and\
           alert.destination_port == self.local_port and\
           alert.source_ip == self.remote_host and\
           abs((alert.date - self.datetime).total_seconds()) <= 1:
               return True
        else:
            return False

    def __repr__(self):
        return str(self.to_dict())

    @staticmethod
    def clean_ipv4(ipaddr):
        """
        Removes hybrid ipv4-6 notation present
        in some addresses.
        """
        ipaddr = ipaddr.replace('::ffff:', '')
        ipaddr = ipaddr.replace('::', '')
        return ipaddr


class Alert(object):
    """
    Represents a Snort alert.
    """

    fields = (
        'header',
        'classification',
        'priority',
        'date',
        'source_ip',
        'destination_ip',
        'destination_port'
    )

    def __init__(self, sensor_uuid, *args):
        try:
            assert len(args) == 8
        except:
            raise ValueError("Unexpected number of attributes.")
        else:
            self.header = args[0]
            self.signature = args[1]
            self.classification = args[2]
            self.priority = args[3]
            # Alert logs don't include year. Creating a datime object
            # with current year.
            date = datetime.strptime(args[4], '%m/%d-%H:%M:%S.%f')
            self.date = datetime(
                    datetime.now().year, date.month, date.day,
                    date.hour, date.minute, date.second, date.microsecond)
            self.source_ip = args[5]
            self.destination_ip = args[6]
            self.destination_port = args[7]
            self.sensor = sensor_uuid

    def __repr__(self):
        return str(self.__dict__)

    def to_dict(self):
        return self.__dict__

    def to_json(self):
        _dict = self.to_dict().copy()
        _dict.update({'date': self.date.strftime("%Y-%m-%dT%H:%M:%S.%f%z")})
        return json.dumps(_dict)

    @classmethod
    def from_log(cls, sensor_uuid, logfile, mindate=None):
        """
        Reads the file logfile and parses out Snort alerts
        from the given alert format.
        Thanks to 'unutbu' at StackOverflow.
        """
        # Defining generic pyparsing objects.
        integer = pyp.Word(pyp.nums)
        ip_addr = pyp.Combine(integer + '.' + integer+ '.' + integer + '.' + integer)
        port = pyp.Suppress(':') + integer
# Defining pyparsing objects from expected format:
        #
        #    [**] [1:160:2] COMMUNITY SIP TCP/IP message flooding directed to SIP proxy [**]
        #    [Classification: Attempted Denial of Service] [Priority: 2]
        #    01/10-00:08:23.598520 201.233.20.33:63035 -> 192.234.122.1:22
        #    TCP TTL:53 TOS:0x10 ID:2145 IpLen:20 DgmLen:100 DF
        #    ***AP*** Seq: 0xD34C30CE  Ack: 0x6B1F7D18  Win: 0x2000  TcpLen: 32
        #
        # Note: This format is known to change over versions.
        # Works with Snort version 2.9.2 IPv6 GRE (Build 78)

        header = (
            pyp.Suppress("[**] [")
            + pyp.Combine(integer + ":" + integer + ":" + integer)
            + pyp.Suppress("]")
        )
        signature = (
            pyp.Combine(pyp.SkipTo("[**]", include=False))
        )
        classif = (
            pyp.Suppress("[**]")
            + pyp.Suppress(pyp.Optional(pyp.Literal("[Classification:")))
            + pyp.Regex("[^]]*") + pyp.Suppress(']')
        )
        pri = pyp.Suppress("[Priority:") + integer + pyp.Suppress("]")
        date = pyp.Combine(
            integer + "/" + integer + '-' + integer + ':' + integer + ':' + integer + '.' + integer
        )
        src_ip = ip_addr + pyp.Suppress(port + "->")
        dest_ip = ip_addr
        dest_port = port
        bnf = header + signature + classif + pri + date + src_ip + dest_ip + dest_port

        alerts = []
        with open(logfile) as snort_logfile:
            for has_content, grp in groupby(
                    snort_logfile, key = lambda x: bool(x.strip())):
                if has_content:
                    content = ''.join(grp)
                    fields = bnf.searchString(content)
                    if fields:
                        if abs(datetime.utcnow() -  datetime.now()).total_seconds() > 1:
                            # Since snort doesn't log in UTC, a correction is needed to
                            # convert the logged time to UTC. The following code calculates
                            # the delta between local time and UTC and uses it to convert
                            # the logged time to UTC. Additional time formatting  makes
                            # sure the previous code doesn't break.
                            date = datetime.strptime(fields[0][4], '%m/%d-%H:%M:%S.%f')
                            date = datetime(
                               datetime.now().year, date.month, date.day,
                               date.hour, date.minute, date.second, date.microsecond)
                            toutc = datetime.utcnow() - datetime.now()
                            date = date + toutc
                            fields[0][4] = date.strftime('%m/%d-%H:%M:%S.%f')
                        alert = cls(sensor_uuid, *fields[0])
                        if (mindate and alert.date > mindate) or not mindate:
                            # If mindate parameter is passed, only newer
                            # alters will be appended.
                            alerts.append(alert)
        return alerts

    @classmethod
    def from_connection(cls, sensor_uuid, conn):
        return Alert(sensor_uuid, 'N/A', '', '', 'N/A',
                     conn.datetime.strftime('%m/%d-%H:%M:%S.%f'),
                     Connection.clean_ipv4(conn.remote_host),
                     Connection.clean_ipv4(conn.local_host),
                     conn.local_port)


class AlertEventHandler(FileSystemEventHandler):

    # Names for the files that will be used to persist
    # the latest connection and alert dates.
    CONN_DATE_FILE = 'conn_date'
    ALERT_DATE_FILE = 'alert_date'

    def __init__(self, sensor_uuid, alert_file,
                 dbpath, on_new_attacks):
        """
        Initializes a filesytem watcher that will watch
        the specified file for changes.
        `alert_file` is the absolute path of the snort alert file.
        `dbpath` is the absolute path of the dionaea sqlite file
        that will be watched.
        `on_new_attacks` is a callback that will get called
        once new alerts are found.
        """
        db_dir = path.dirname(dbpath)
        self.alert_file = alert_file
        self.dbpath = dbpath
        self.observer = Observer()
        self.observer.schedule(self, db_dir, False)
        self._on_new_attacks = on_new_attacks
        self.sensor_uuid = sensor_uuid
        self.engine = create_engine(
                'sqlite:///{}'.format(self.dbpath), echo=False)
        self.session = sessionmaker(bind=self.engine)()

    @staticmethod
    def _unpickle(dfile):
        try:
            return pickle.load(open(dfile, 'r'))
        except IOError:
            return None

    @staticmethod
    def _pickle(dfile, date):
        pickle.dump(date, open(dfile, 'w'))

    @property
    def latest_alert_date(self):
        return AlertEventHandler._unpickle(AlertEventHandler.ALERT_DATE_FILE)

    @latest_alert_date.setter
    def latest_alert_date(self, date):
        AlertEventHandler._pickle(
                AlertEventHandler.ALERT_DATE_FILE, date)

    @property
    def latest_conn_date(self):
        return AlertEventHandler._unpickle(AlertEventHandler.CONN_DATE_FILE)

    @latest_conn_date.setter
    def latest_conn_date(self, date):
        AlertEventHandler._pickle(
                AlertEventHandler.CONN_DATE_FILE, date)

    def query_connections(self, mindate=None):
        conns = self.session.query(Connection)
        if mindate:
            # Calculate UNIX timestamp of mindate.
            mindatestamp = (mindate - datetime(1970, 1, 1)).total_seconds()
            conns = conns.filter(
                Connection.connection_timestamp > mindatestamp)
        conns = conns.order_by(Connection.connection)
        return conns

    def on_any_event(self, event):
        if (not event.event_type == 'deleted') and\
           (event.src_path == self.dbpath):
            alerts = Alert.from_log(self.sensor_uuid, self.alert_file,
                                    self.latest_alert_date)
            conns = self.query_connections(self.latest_conn_date)
            if alerts:
                alerts.sort(key=lambda e: e.date)
                self.latest_alert_date = alerts[-1].date
            if conns.count() > 0:
                latest_conn = self.session.query(Connection).order_by(
                     Connection.connection_timestamp.desc()).first()
                self.latest_conn_date = latest_conn.datetime

                # attacks will be a list of merged conns and alerts.
                attacks = []
                for conn in conns:
                    matched_alert = None
                    for al in alerts:
                        if conn.is_alert(al):
                            matched_alert = al
                            # Alert is a more complete object, so it will
                            # be used as our attack model.
                            attacks.append(al)
                            break
                    else:
                        # Connection didn't match any alerts, will be appended as is.
                        attacks.append(Alert.from_connection(self.sensor_uuid, conn))
                    if matched_alert:
                        # Popping matched items to eliminate alert iterations
                        # on next cycle.
                        alerts.pop(matched_alert)
                # Finally append any alerts that didn't match.
                attacks.extend(alerts)
                self._on_new_attacks(attacks)


def config_logger(logfile, daemon):
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s  -  %(name)s - %(message)s')
    if not daemon:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(formatter)
        logger.addHandler(console)
    if logfile:
        from logging.handlers import RotatingFileHandler

        rotatelog = RotatingFileHandler(
                logfile, maxBytes=10240, backupCount=5)
        rotatelog.setLevel(logging.INFO)
        rotatelog.setFormatter(formatter)
        logger.addHandler(rotatelog)


if __name__ ==  '__main__':
    args = docopt(__doc__, version='MHNClient 0.0.1')
    daemonize = args.get('-D')
    with open(args.get('-c')) as config:
        try:
            configdict = json.loads(config.read())
        except ValueError:
            raise SystemExit("Error parsing config file.")
    app_dir = configdict.get('app_dir')
    log_file = path.join(app_dir, 'var/log/mhnclient.log')
    pid_file = path.join(app_dir, 'var/run/mhn.pid')
    honeypot = MHNClient(**configdict)
    for fpath in map(path.dirname, [log_file, pid_file]):
        if not path.exists(fpath):
            # Create needed directories.
            makedirs(fpath)
    config_logger(log_file, daemonize)
    honeypot.run()
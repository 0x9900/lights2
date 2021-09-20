#!/usr/bin/env python3.7
#
# pylint: disable=missing-docstring

import argparse
import logging
import os
import pickle
import random
import signal
import sys
import time

from datetime import datetime
from datetime import timedelta

import RPi.GPIO as gpio
import json
import pytz
import requests

logging.basicConfig(format='%(asctime)s %(levelname)s[%(process)d]: %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.INFO)

SLEEP_TIME = 60
CONFIG_FILE = '/etc/lights.json'
EPHEMERIDES_FILE = '/tmp/ephemerides.pkl'
MANDATORY_FIELDS = {'ports', 'local_tz', 'latitude', 'longitude', 'taskfile'}

class Config:
  _instance = None
  config_data = None
  def __new__(cls, *args, **kwargs):
    if cls._instance is None:
      cls._instance = super(Config, cls).__new__(cls)
      cls._instance.config_data = {}
    return cls._instance

  def __init__(self, config_file=CONFIG_FILE):
    if self.config_data:
      return
    logging.debug('Reading config file')
    if not os.path.exists(config_file):
      logging.error('Configuration file "%s" not found', config_file)
      sys.exit(os.EX_CONFIG)

    try:
      with open(config_file, 'r') as confd:
        lines = []
        for line in confd:
          line = line.strip()
          if not line or line.startswith('#'):
            continue
          lines.append(line)
        self.config_data = json.loads('\n'.join(lines))
    except ValueError as err:
      logging.error('Configuration error: "%s"', err)
      sys.exit(os.EX_CONFIG)

    missing_fields = self.config_data.keys() ^ MANDATORY_FIELDS
    if missing_fields != set():
      logging.error('Configuration keys "%s" are missing', missing_fields)
      sys.exit(os.EX_CONFIG)

  def __getattr__(self, attr):
    if attr not in self.config_data:
      raise AttributeError("'{}' object has no attribute '{}'".format(self.__class__, attr))
    return self.config_data[attr]


class Lights:

  def __init__(self, ports):
    self._ports = ports
    gpio.setwarnings(False)
    gpio.setmode(gpio.BCM)
    for port in self._ports:
      gpio.setup(port, gpio.OUT)

  def off(self, ports, sleep=0.5):
    for port in ports:
      if port not in self._ports:
        continue
      gpio.output(port, gpio.HIGH)
      time.sleep(.25)

  def on(self, ports, sleep=0.25):
    for port in ports:
      if port not in self._ports:
        continue
      gpio.output(port, gpio.LOW)
      time.sleep(.25)

  def __str__(self):
    status = self.status().items()
    return ', '.join([f"{k:02d}:{v}" for k, v in status])

  def status(self):
    status = {}
    st_msg = {0: 'ON', 1: 'Off'}
    for port in self._ports:
      status[port] = st_msg[gpio.input(port)]
    return status


def ephemerides(lat, lon, timez):
  """Get the sunset and runrise information"""
  try:
    st_mtime = os.stat(EPHEMERIDES_FILE).st_mtime
  except FileNotFoundError:
    st_mtime = None

  if st_mtime is not None and st_mtime + 86400 > time.time():
    with open(EPHEMERIDES_FILE, 'rb') as fdsun:
      suninfo = pickle.loads(fdsun.read())
    return suninfo

  logging.info('Download Ephemerides')
  now = datetime.now()
  params = dict(lat=lat, lng=lon, formatted=0, date=now.strftime('%Y-%m-%d'))
  url = 'https://api.sunrise-sunset.org/json'
  try:
    resp = requests.get(url=url, params=params, timeout=(3, 10))
    data = resp.json()
  except Exception as err:
    logging.error(err)          # Error reading the sun info, return yesterday's values
    with open(EPHEMERIDES_FILE, 'rb') as fdsun:
      suninfo = pickle.loads(fdsun.read())
    return suninfo

  tzone = pytz.timezone(timez)
  suninfo = {}
  for key, val in data['results'].items():
    if key == 'day_length':
      suninfo[key] = val
    else:
      suninfo[key] = datetime.fromisoformat(val).astimezone(tzone)

  with open(EPHEMERIDES_FILE, 'wb') as fdsun:
    fdsun.write(pickle.dumps(suninfo))

  return suninfo


def check_status(lights, ports=None):
  status = lights.status(ports)
  logging.info(', '.join([f"{k:02d}:{v}" for k, v in status.items()]))


def build_task(config):
  """
  format: "[lights] : start_time : end_time : week_day"
  """
  tasks = []
  sun = ephemerides(config.latitude, config.longitude, config.local_tz)

  try:
    fdt = open(config.taskfile, 'r')
  except (AttributeError, FileNotFoundError) as err:
    logging.error(err)
    return tasks

  for line in fdt:
    line = line.strip()
    if not line or line.startswith('#'):
      continue

    _lights, _start, _end, _days = line.split()
    if _lights == '*':
      ports = config.ports
    elif _lights.startswith('[') and _lights.endswith(']'):
      lights = [int(l) - 1 for l in _lights[1:-1].split(',')]
      ports = [config.ports[l] for l in lights]
    elif _lights.isdigit():
      ports = [config.ports[int(_lights) - 1]]

    if _start in sun:
      start = int(sun[_start].strftime('%H%M'))
    elif ':' in _start:
      start = int(_start.replace(':', ''))

    if _end in sun:
      end = int(sun[_end].strftime('%H%M'))
    elif ':' in _end:
      end = int(_end.replace(':', ''))

    if _days == '*':
      days = range(7)
    elif _days.startswith('['):
      days = [int(l) for l in _days[1:-1].split(',')]
    elif _days.isdigit():
      days = [int(_days)]

    tasks.append((ports, start, end, days))

  fdt.close()
  return tasks

def run_tasks(tasks, _ports, lights):
  # this is to chance the current lights state. The we will only log when the state change.
  if not hasattr(run_tasks, '_previous_state'):
    run_tasks._previous_state = {}

  if not tasks:
    return

  now = datetime.now()
  day = now.weekday()
  tod = int(now.strftime('%H%M'))
  actions = {p: False for p in _ports}

  for ports, start, end, days in tasks:
    if start > end:
      end += 2400
    if start < tod < end and day in days:
      for port in ports:
        actions[port] = True

  lights.off([p for p, a in actions.items() if not a])
  lights.on([p for p, a in actions.items() if a])

  if run_tasks._previous_state != actions:
    run_tasks._previous_state = actions
    logging.info("%s", lights)


def main():
  parser = argparse.ArgumentParser(description='Garden lights')
  parser.add_argument('--config-file', default=CONFIG_FILE, help='Turn off all the lights')
  pargs = parser.parse_args()

  config = Config(pargs.config_file)
  lights = Lights(config.ports)
  while True:
    try:
      tasks = build_task(config)
    except (UnboundLocalError, ValueError):
      logging.error('%s Syntax error', config.taskfile)
      sys.exit(os.EX_CONFIG)
    except IndexError:
      logging.error('Ports error %s', config.taskfile)
      sys.exit(os.EX_CONFIG)

    run_tasks(tasks, config.ports, lights)

    wait_time = 59 - datetime.now().second
    logging.debug('wait_time: %s', wait_time)
    time.sleep(wait_time)
    while datetime.now().second > 0: # wait for the start of the minute
      logging.debug('wait... %s', datetime.now())
      time.sleep(0.5)

if __name__ == "__main__":
  main()

#!/usr/bin/env python

from __future__ import print_function
import sys
import time
import json
import signal
import inspect
import logging
import importlib
from path import path
import logging.config


class Manager(object):
    """
    Manages polling connections to XQueue.
    """
    def __init__(self):
        self.clients = []
        self.poll_time = 10
        self.log = logging
        self.http_basic_auth = None

    def client_from_config(self, queue_name, config):
        """
        Return an XQueueClient from the configuration object.
        """
        from . import client

        klass = getattr(client, config.get('CLASS', 'XQueueClientThread'))
        watcher = klass(queue_name,
                        xqueue_server=config.get('SERVER', 'http://localhost:18040'),
                        xqueue_auth=config.get('AUTH', (None, None)),
                        http_basic_auth=self.http_basic_auth)

        for handler_config in config.get('HANDLERS', []):
            handler_name = handler_config['HANDLER']
            mod_name, classname = handler_name.rsplit('.', 1)
            module = importlib.import_module(mod_name)

            kw = handler_config.get('KWARGS', {})

            # codejail configuration per handler
            codejail_config = handler_config.get("CODEJAIL", None)
            if codejail_config:
                kw['codejail_python'] = self.enable_codejail(codejail_config)

            handler = getattr(module, classname)
            if kw or inspect.isclass(handler):
                # handler could be a function or a class
                handler = handler(**kw)
            watcher.add_handler(handler)
        return watcher

    def configure(self, configuration):
        """
        Configure XQueue clients.
        """
        for queue_name, config in configuration.items():
            for i in range(config.get('CONNECTIONS', 1)):
                watcher = self.client_from_config(queue_name, config)
                self.clients.append(watcher)

    def configure_from_directory(self, directory):
        """
        Load configuration files from the config_root
        and one or more queue configurations from a conf.d
        directory relative to the config_root
        """
        directory = path(directory)

        log_config = directory / 'logging.json'
        if log_config.exists():
            with open(log_config) as config:
                logging.config.dictConfig(json.load(config))
        else:
            logging.basicConfig(level="DEBUG")
        self.log = logging.getLogger('xqueue_watcher.manager')

        app_config = directory / 'xqwatcher.json'

        if app_config.exists():
            with open(app_config) as config:
                config_tokens = json.load(config)
                self.http_basic_auth = config_tokens.get('HTTP_BASIC_AUTH',None)
                self.poll_time = config_tokens.get("POLL_TIME",10)

        confd = directory / 'conf.d'

        for watcher in confd.files('*.json'):
            with open(watcher) as queue_config:
                self.configure(json.load(queue_config))

    def enable_codejail(self, codejail_config):
        """
        Enable codejail for the process.
        codejail_config is a dict like this:
        {
            "name": "python",
            "python_bin": "/path/to/python",
            "user": "sandbox_username",
            "limits": {
                "CPU": 1,
                ...
            }
        }
        limits are optional
        user defaults to the current user
        """
        import codejail.jail_code
        import getpass
        name = codejail_config["name"]
        python_bin = codejail_config['python_bin']
        user = codejail_config.get('user', getpass.getuser())

        codejail.jail_code.configure(name, python_bin, user=user)
        limits = codejail_config.get("limits", {})
        for name, value in limits.items():
            codejail.jail_code.set_limit(name, value)
        self.log.info("configured codejail -> %s %s %s", name, python_bin, user)
        return name

    def start(self):
        """
        Start XQueue client threads (or processes).
        """
        for c in self.clients:
            self.log.info('Starting %r', c)
            c.start()

    def wait(self):
        """
        Monitor clients.
        """
        if not self.clients:
            return
        signal.signal(signal.SIGTERM, self.shutdown)
        while 1:
            for client in self.clients:
                if not client.is_alive():
                    self.log.error('Client died -> %r',
                                   client.queue_name)
                    self.shutdown()
                try:
                    time.sleep(self.poll_time)
                except KeyboardInterrupt:  # pragma: no cover
                    self.shutdown()

    def shutdown(self, *args):
        """
        Cleanly shutdown all clients.
        """
        self.log.info('shutting down')
        while self.clients:
            client = self.clients.pop()
            client.shutdown()
            if client.processing:
                try:
                    client.join()
                except RuntimeError:
                    self.log.exception("joining")
                    sys.exit(9)
            self.log.info('%r done', client)
        self.log.info('done')
        sys.exit()


def main(args=None):
    import argparse
    parser = argparse.ArgumentParser(prog="xqueue_watcher", description="Run grader from settings")
    parser.add_argument('-d', '--config_root', required=True,
                        help='Configuration root from which to load general '
                             'watcher configuration. Queue configuration '
                             'is loaded from a conf.d directory relative to '
                             'the root')
    args = parser.parse_args(args)

    manager = Manager()
    manager.configure_from_directory(args.config_root)

    if not manager.clients:
        print("No xqueue watchers configured")
    manager.start()
    manager.wait()
    return 0
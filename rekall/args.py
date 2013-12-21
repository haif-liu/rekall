#!/usr/bin/python

# Rekall Memory Forensics
# Copyright (C) 2012 Michael Cohen <scudette@gmail.com>
# Copyright 2013 Google Inc. All Rights Reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#

__author__ = "Michael Cohen <scudette@gmail.com>"

"""This module manages the command line parsing logic."""

import argparse
import logging
import re
import os
import sys
import zipfile

from rekall import config
from rekall import constants
from rekall import plugin
from rekall import session
from rekall import utils


config.DeclareOption("--plugin", default=[], nargs="+",
                     help="Load user provided plugin bundle.")

config.DeclareOption("--output",
                     help="Write to this output file.")

config.DeclareOption(
    "-h", "--help", default=False, action="store_true",
    help="Show help about global paramters.")

config.DeclareOption("--overwrite", action="store_true", default=False,
                     help="Allow overwriting of output files.")


class IntParser(argparse.Action):
    """Class to parse ints either in hex or as ints."""
    def parse_int(self, value):
        # Support suffixes
        multiplier = 1
        m = re.search("(.*)(mb|kb|m|k)", value)
        if m:
            value = m.group(1)
            suffix = m.group(2).lower()
            if suffix in ("mb", "m"):
                multiplier = 1024 * 1024
            elif suffix in ("kb", "k"):
                multiplier = 1024

        try:
            if value.startswith("0x"):
                value = int(value, 16) * multiplier
            else:
                value = int(value) * multiplier
        except ValueError:
            raise argparse.ArgumentError(self, "Invalid integer value")

        return value

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, self.parse_int(values))


class ArrayIntParser(IntParser):
    """Parse input as a comma separated list of integers.

    We support input in the following forms:

    --pid 1,2,3,4,5

    --pid 1 2 3 4 5

    --pid 0x1 0x2 0x3
    """

    def __call__(self, parser, namespace, values, option_string=None):
        result = []
        if isinstance(values, basestring):
            values = [values]

        for value in values:
            result.extend([self.parse_int(x) for x in value.split(",")])

        setattr(namespace, self.dest, result)


class ArrayStringParser(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        result = []
        if isinstance(values, basestring):
            values = [values]

        for value in values:
            result.extend([x for x in value.split(",")])

        setattr(namespace, self.dest, result)


class MockArgParser(object):
    def add_argument(self, short_flag="", long_flag="", dest="", **kwargs):
        if short_flag.startswith("--"):
            flag = short_flag
        elif long_flag.startswith("--"):
            flag = long_flag
        elif dest:
            flag = dest
        else:
            flag = short_flag

        # This function will be called by the args() class method, and we just
        # keep track of the args this module defines.
        arg_name = flag.strip("-").replace("-", "_")

        self.args[arg_name] = None

    def build_args_dict(self, cls, namespace):
        """Build a dict suitable for **kwargs from the namespace."""
        self.args = {}

        # Discover all the args this module uses.
        cls.args(self)

        for key in self.args:
            self.args[key] = getattr(namespace, key)

        return self.args


class RekallArgParser(argparse.ArgumentParser):
    ignore_errors = False

    def error(self, message):
        if self.ignore_errors:
            return

        # We trap this error especially since we launch the volshell.
        if message == "too few arguments":
            return

        super(RekallArgParser, self).error(message)

    def parse_known_args(self, args=None, namespace=None, force=False):
        self.ignore_errors = force

        result = super(RekallArgParser, self).parse_known_args(
            args=args, namespace=namespace)

        return result

    def print_help(self, file=None):
        if self.ignore_errors:
            return

        return super(RekallArgParser, self).print_help(file=file)

    def exit(self, *args, **kwargs):
        if self.ignore_errors:
            return

        return super(RekallArgParser, self).exit(*args, **kwargs)


def LoadPlugins(paths=None):
    PYTHON_EXTENSIONS = [".py", ".pyo", ".pyc"]

    for path in paths:
        if not os.access(path, os.R_OK):
            logging.error("Unable to find %s", path)
            continue

        path = os.path.abspath(path)
        directory, filename = os.path.split(path)
        module_name, ext = os.path.splitext(filename)

        # Its a python file.
        if ext in PYTHON_EXTENSIONS:
            # Make sure python can find the file.
            sys.path.insert(0, directory)

            try:
                logging.info("Loading user plugin %s", path)
                __import__(module_name)
            except Exception, e:
                logging.error("Error loading user plugin %s: %s", path, e)
            finally:
                sys.path.pop(0)

        elif ext == ".zip":
            zfile = zipfile.ZipFile(path)

            # Make sure python can find the file.
            sys.path.insert(0, path)
            try:
                logging.info("Loading user plugin archive %s", path)
                for name in zfile.namelist():
                    # Change from filename to python package name.
                    module_name, ext = os.path.splitext(name)
                    if ext in PYTHON_EXTENSIONS:
                        module_name = module_name.replace("/", ".").replace(
                            "\\", ".")

                        try:
                            __import__(module_name.strip("\\/"))
                        except Exception as e:
                            logging.error("Error loading user plugin %s: %s",
                                          path, e)

            finally:
                sys.path.pop(0)

        else:
            logging.error("Plugin %s has incorrect extension.", path)


def LoadProfileIntoSession(parser, argv, user_session):
    # Figure out the profile
    known_args, unknown_args = parser.parse_known_args(args=argv)

    # Force debug level logging with the verbose flag.
    if getattr(known_args, "verbose", None):
        known_args.logging = "DEBUG"

    with user_session.state as state:
        config.MergeConfigOptions(state)

        for arg, value in known_args.__dict__.items():
            state.Set(arg, value)

    if user_session.profile is None:
        guesser = user_session.plugins.guess_profile()
        guesser.update_session()

    # Now load the third party user plugins. These may introduce additional
    # plugins with args.
    LoadPlugins(user_session.state.plugin)


def parse_args(argv=None, user_session=None):
    """Parse the args from the command line argv."""
    parser =  RekallArgParser(
        description=constants.BANNER,
        conflict_handler='resolve',
        add_help=False,
        epilog='When no module is provided, drops into interactive mode',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    config.RegisterArgParser(parser)

    # First load the profile to enable the module selection (which depends on
    # the profile).
    LoadProfileIntoSession(parser, argv, user_session)

    # Add module specific args.
    subparsers = parser.add_subparsers(
        description="The following plugins can be selected.",
        metavar='Plugin',
        )

    parsers = {}

    # Add module specific parser for each module.
    classes = []
    for cls in plugin.Command.classes.values():
        if (cls.name and cls.is_active(user_session) and not
            cls._interactive):
            classes.append(cls)

    for cls in sorted(classes, key=lambda x: x.name):
        docstring = cls.__doc__ or " "
        doc = docstring.splitlines()[0] or " "
        name = cls.name
        try:
            module_parser = parsers[name]
        except KeyError:
            parsers[name] = module_parser = subparsers.add_parser(
                cls.name, help=doc, description=docstring)

            cls.args(module_parser)
            module_parser.set_defaults(module=cls.name)

    # Parse the final command line.
    result = parser.parse_args(argv)

    # We handle help especially since we want to enumerate all plugins.
    if getattr(result, "help", None):
        parser.print_help()
        sys.exit(-1)

    return result


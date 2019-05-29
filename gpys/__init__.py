import configparser
import logging
import os
import re
import socket
import sys
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import dispy
import psycopg2
from psycopg2 import sql

logger = logging.getLogger('gpys')


class Connection:
    """
    Ideally this class is to be used as a direct connection to the gnss_data database.  As such it should initiate to
    a pgdb.Connection object through the uses of pgdb.connect().  Once the object has established a connection to the
    database, class methods should be used to interact with the database.  The goal is to abstract all the SQL commands
    so we can deal with the errors here instead of out in the wild.
    """

    __slots__ = ['conn',
                 'logger']

    def __init__(self, config, parent_logger='archive'):

        # Open connection to server
        connect_dsn = None
        try:
            self.logger = logging.getLogger('.'.join([parent_logger, 'gpys.Connection']))
            self.logger.debug('Initializing Connection object.')
            self.conn = None
            connect_dsn = 'dbname={} host={} user={} password={}'.format(config['database'],
                                                                         config['hostname'],
                                                                         config['username'],
                                                                         config['password'])
            self.conn = psycopg2.connect(connect_dsn)
            self.logger.debug('Connection established: {}'.format(self.conn.dsn))
            self.insert('executions', {'script': parent_logger})
        except Exception as e:
            self.logger.error('Uncaught exception: {}'.format(e))
            sys.exit(1)
        finally:
            del connect_dsn, config, parent_logger

    def __del__(self):
        if self.conn:
            self.conn.close()
            self.logger.debug('Connection closed: {}'.format(self.conn.dsn))

    def _execute_wrapper(self, sql_statement, values=None, retval=False, return_dict=True):
        """
        Deal with all the actual database interactions here and deal with the related error possibilities.
        :param sql_statement: A composable object.
        :param values: List or tuple of values for the sql statement.
        :param retval: Whether to return a value or not.
        :param return_dict: Return it as a dictionary or not.
        :return: Returns either a list of tuples containing the results or a dictionary.
        """
        try:
            with self.conn.cursor() as curs:
                if values is not None:
                    curs.execute(sql_statement, values)
                else:
                    curs.execute(sql_statement)
                if retval:
                    if return_dict:
                        qresults = curs.fetchall()
                        keys = [name[0] for name in curs.description]
                        d = defaultdict(list)
                        for n, key in enumerate(keys):
                            for rec in qresults:
                                if type(rec[n]) != Decimal:
                                    d[key].append(rec[n])
                                else:
                                    d[key].append(float(rec[n]))
                        return d
                    else:

                        return curs.fetchall()
            self.conn.commit()
        except Exception as e:
            self.logger.error('Uncaught exception: {}'.format(e))
            sys.exit(1)

    def insert(self, table, record: dict):
        x = sql.SQL('{}').format(sql.Identifier(table))
        y = sql.SQL(', ').join([sql.Identifier(key) for key in record.keys()])
        z = sql.SQL(', ').join(sql.Placeholder() * len(record))
        insert_statement = sql.SQL("INSERT INTO {0} ({1}) VALUES ({2});").format(x, y, z)
        self._execute_wrapper(insert_statement, [v for v in record.values()])

    def update_locks(self):

        clear_statement = sql.SQL("DELETE FROM locks WHERE {} NOT LIKE {};").format(sql.Identifier('NetworkCode'),
                                                                                    sql.Placeholder())
        self._execute_wrapper(clear_statement, ('?%',))

    def load_table(self, table):
        select_statement = sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
        return self._execute_wrapper(select_statement, retval=True)

    def clear_locked(self, table):

        clear_statement = sql.SQL("DELETE FROM {} WHERE {} LIKE {};").format(sql.Identifier(table),
                                                                             sql.Identifier('NetworkCode'),
                                                                             sql.Placeholder())
        self._execute_wrapper(clear_statement, ('?%',))

    def load_tankstruct(self):
        sql_statement = sql.SQL('SELECT * FROM {0} INNER JOIN {1} '
                                'USING ({2}) ORDER BY {3}').format(sql.Identifier('rinex_tank_struct'),
                                                                   sql.Identifier('keys'),
                                                                   sql.Identifier('KeyCode'),
                                                                   sql.Identifier('Level'))
        return self._execute_wrapper(sql_statement, retval=True)

    def insert_event(self, event):
        event_dict = event.db_dict()
        y = sql.SQL(', ').join([sql.Identifier(key) for key in event_dict.keys()])
        z = sql.SQL(', ').join(sql.Placeholder() * len(event_dict))
        insert_statement = sql.SQL("insert into {0} ({1}) VALUES ({2});").format(sql.Identifier('events'), y, z)
        self._execute_wrapper(insert_statement, [v for v in event_dict.values()])

    def print_summary(self, script):
        script_start = sql.SQL('SELECT MAX({}) AS mx FROM {} WHERE {} = {}').format(sql.Identifier('exec_date'),
                                                                                    sql.Identifier('executions'),
                                                                                    sql.Identifier('script'),
                                                                                    sql.Placeholder())
        st = self._execute_wrapper(script_start, (script,), retval=True)

        counter = sql.SQL(
            'SELECT COUNT(*) AS cc FROM {0} WHERE {1} >= {2} AND {3} = {2}').format(sql.Identifier('events'),
                                                                                    sql.Identifier('EventDate'),
                                                                                    sql.Placeholder(),
                                                                                    sql.Identifier('EventType'))
        info = self._execute_wrapper(counter, (st[0][0], 'info'), retval=True)
        erro = self._execute_wrapper(counter, (st[0][0], 'error'), retval=True)
        warn = self._execute_wrapper(counter, (st[0][0], 'warning'), retval=True)

        print(' >> Summary of events for this run:')
        print(' -- info    : %i' % info[0][0])
        print(' -- errors  : %i' % erro[0][0])
        print(' -- warnings: %i' % warn[0][0])

    def spatial_check(self, vals, search_in_new=False):
        if not search_in_new:
            where_clause = sql.SQL('WHERE {} NOT LIKE {}').format(sql.Identifier('NetworkCode'),
                                                                  sql.Literal('?%%'))
        else:
            where_clause = sql.SQL('')
        sql_select = sql.SQL(
            'SELECT {0} FROM (SELECT *, 2*ASIN(SQRT(SIN((RADIANS({1})-RADIANS({2}))/2)^2 + COS(RADIANS({2}))'
            '* COS(RADIANS({1})) * SIN((RADIANS({1}) - RADIANS({3}))/2)^2))*6371000 AS distance FROM '
            '{4} {5}) AS st1 LEFT JOIN {4} AS st2 ON st1.{6} = st2.{6} AND st1.{7} = st2.{7} AND '
            'st1.distance < COALESCE(st2.{8}, 20) WHERE st2.{7} IS NOT NULL').format(
            sql.SQL(', ').join([sql.SQL('st1.{}').format(sql.Identifier('NetworkCode')),
                                sql.SQL('st1.{}').format(sql.Identifier('StationCode')),
                                sql.SQL('st1.{}').format(sql.Identifier('StationName')),
                                sql.SQL('st1.{}').format(sql.Identifier('DateStart')),
                                sql.SQL('st1.{}').format(sql.Identifier('DateEnd')),
                                sql.SQL('st1.{}').format(sql.Identifier('auto_x')),
                                sql.SQL('st1.{}').format(sql.Identifier('auto_y')),
                                sql.SQL('st1.{}').format(sql.Identifier('auto_z')),
                                sql.SQL('st1.{}').format(sql.Identifier('Harpos_coeff_otl')),
                                sql.SQL('st1.{}').format(sql.Identifier('lat')),
                                sql.SQL('st1.{}').format(sql.Identifier('lon')),
                                sql.SQL('st1.{}').format(sql.Identifier('height')),
                                sql.SQL('st1.{}').format(sql.Identifier('max_dist')),
                                sql.SQL('st1.{}').format(sql.Identifier('dome')),
                                sql.SQL('st1.distance')]),
            sql.Placeholder(),
            sql.Identifier('lat'),
            sql.Identifier('lon'),
            sql.Identifier('stations'),
            where_clause,
            sql.Identifier('StationCode'),
            sql.Identifier('NetworkCode'),
            sql.Identifier('max_dist'))
        return self._execute_wrapper(sql_select, vals, retval=True)

    def nearest_station(self, vals, search_in_new=False):
        if not search_in_new:
            where_clause = sql.SQL('WHERE {} NOT LIKE {}').format(sql.Identifier('NetworkCode'),
                                                                  sql.Literal('?%%'))
        else:
            where_clause = sql.SQL('')
        sql_select = sql.SQL(
            'SELECT * FROM (SELECT *, 2*ASIN(SQRT(SIN((RADIANS({0})-RADIANS({1}))/2)^2 + COS(RADIANS({1}))'
            '* COS(RADIANS({0})) * SIN((RADIANS({0}) - RADIANS({2}))/2)^2))*6371000 AS distance FROM '
            '{3} {4}) AS dd ORDER BY distance').format(
            sql.Placeholder(),
            sql.Identifier('lat'),
            sql.Identifier('lon'),
            sql.Identifier('stations'),
            where_clause)
        return self._execute_wrapper(sql_select, vals, retval=True)

    def similar_locked(self, vals):
        sql_select = sql.SQL(
            'SELECT * FROM (SELECT *, 2*ASIN(SQRT(SIN((RADIANS({0})-RADIANS({1}))/2)^2 + COS(RADIANS({1}))'
            '* COS(RADIANS({0})) * SIN((RADIANS({0}) - RADIANS({2}))/2)^2))*6371000 AS distance FROM '
            '{3} WHERE {4} LIKE {5} AND {6} LIKE {0}) AS dd WHERE distance <= 100').format(
            sql.Placeholder(),
            sql.Identifier('lat'),
            sql.Identifier('lon'),
            sql.Identifier('stations'),
            sql.Identifier('NetworkCode'),
            sql.Literal('?%%'),
            sql.Identifier('StationCode'))

        return self._execute_wrapper(sql_select, vals, retval=True)

    def update(self, table: str, row: dict, record: dict):
        a = sql.SQL('{}').format(sql.Identifier(table))
        b = sql.SQL(', ').join([sql.Identifier(key) for key in record.keys()])
        c = sql.SQL(', ').join(sql.Placeholder() * len(record))
        d = sql.SQL(', ').join([sql.Identifier(key) for key in row.keys()])
        e = sql.SQL(', ').join(sql.Placeholder() * len(row))
        insert_statement = sql.SQL("UPDATE {0} SET ({1}) = ({2}) WHERE ({3}) LIKE ({4})").format(a, b, c, d, e)
        vals = []
        for v in record.values():
            vals.append(v)
        for v in row.values():
            vals.append(v)

        self._execute_wrapper(insert_statement, vals)

    def load_table_matching(self, table: str, where_dict: dict):
        select_statement = sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
        where_statement = sql.SQL("WHERE")
        if len(where_dict) > 1:
            like_statement = sql.SQL(' AND ').join(
                [sql.SQL(f'{sql.Identifier(k)} = {sql.Placeholder()}') for k in where_dict.keys()])
        else:
            like_statement = [sql.SQL(f'{sql.Identifier(k)} LIKE {sql.Placeholder()}') for k in where_dict.keys()]
            like_statement = like_statement[0]

        full_statement = sql.SQL(' ').join([select_statement, where_statement, like_statement])
        return self._execute_wrapper(full_statement, [v for v in where_dict.values()], retval=True)


class ReadOptions:
    """
    Class that deals with reading in the default configuration file gnss_data.cfg
    """

    def __init__(self,
                 configfile='gnss_data.cfg', parent_logger='archive'):
        # Initialize the logger.
        rologger = logging.getLogger('.'.join([parent_logger, 'gpys.ReadOptions']))
        rologger.debug('Initializing ReadOptions object from {}'.format(configfile))
        conn = None
        self.options = {'path': None,
                        'repository': None,
                        'parallel': False,
                        'node_list': None,
                        'brdc': None,
                        'sp3_type_1': None,
                        'sp3_type_2': None,
                        'sp3_type_3': None,
                        'sp3_altr_1': None,
                        'sp3_altr_2': None,
                        'sp3_altr_3': None,
                        'grdtab': None,
                        'otlgrid': None,
                        'otlmodel': 'FES2014b',
                        'ppp_path': None,
                        'institution': None,
                        'info': None,
                        'sp3': None,
                        'frames': None,
                        'atx': None,
                        'height_codes': None,
                        'ppp_exe': None,
                        'ppp_remote_local': (),
                        'gg': None}

        fp, p, config, section = None, None, None, None
        try:
            # Read in the config file.
            rologger.debug('Checking config_file: {}'.format(configfile))
            config = configparser.ConfigParser()
            with open(configfile) as fp:
                config.read_file(fp)

            # Parse the sections into the options dict.
            for section in config.sections():
                for key in config[section]:
                    self.options[key] = config[section][key]

            rologger.debug('{} read into program, now checking file paths.'.format(configfile))
            paths = [self.options['path'], self.options['repository'], self.options['brdc'],
                     self.options['sp3'], self.options['gg'],
                     self.options['ppp_path'], self.options['atx']]
            for p in paths:
                rologger.debug('Checking {}'.format(Path(p).parent))
                if not Path(p).parent.exists():
                    raise FileNotFoundError

        except FileNotFoundError:
            rologger.error('FileNotFoundError: {}'.format(p), exc_info=sys.exc_info())
            sys.exit(1)
        except Exception as e:
            rologger.error('Uncaught Exception: {} {}'.format(type(e), e), exc_info=sys.exc_info(), stack_info=True)
            sys.exit(1)
        finally:
            del fp, p, section
        rologger.debug('Parent directories are okay, some of the files might not exist though.')

        # Frames and dates
        # TODO: Implement the frames.
        frame, atx = None, None
        try:
            rologger.debug('Building the reference frames.')
            rologger.debug('Reference frames built.')
        except KeyError:
            rologger.error('The frames were not correctly defined in the config file {}'.format(configfile),
                           exc_info=sys.exc_info())
            sys.exit(1)
        except Exception as e:
            rologger.error('Uncaught Exception: {} {}'.format(type(e), e), exc_info=sys.exc_info(), stack_info=True)
            sys.exit(1)
        finally:
            del frame, atx

        try:
            rologger.debug('Creating some properties for ease of use.')
            self.data_in = Path(self.options['repository']) / Path('data_in')
            self.data_in_retry = Path(self.options['repository']) / Path('data_in_retry')
            self.data_reject = Path(self.options['repository']) / Path('data_rejected')
            os.makedirs(self.data_in, exist_ok=True)
            os.makedirs(self.data_in_retry, exist_ok=True)
            os.makedirs(self.data_reject, exist_ok=True)
            self.sp3types = [self.options['sp3_type_1'], self.options['sp3_type_2'], self.options['sp3_type_3']]
            self.sp3types = [sp3type for sp3type in self.sp3types if sp3type is not None]
            # alternative sp3 types
            self.sp3altrn = [self.options['sp3_altr_1'], self.options['sp3_altr_2'], self.options['sp3_altr_3']]
            self.sp3altrn = [sp3alter for sp3alter in self.sp3altrn if sp3alter is not None]
            if self.options['parallel'] == 'True':
                self.options['parallel'] = True
            else:
                self.options['parallel'] = False
            self.options['node_list'] = self.options['node_list'].strip(' ').split(',')
        except KeyError as e:
            rologger.error(e)
            sys.exit(1)
        except OSError as e:
            rologger.error(e)
            sys.exit(1)
        except Exception as e:
            rologger.error(e)
            sys.exit(1)
        if self.options['parallel']:
            rologger.debug('Testing JobServer connection.')
            try:
                JobServer(self.options).cluster_test()
            except Exception as e:
                rologger.error('Uncaught Exception: {} {}'.format(type(e), e))
                sys.exit(1)
            rologger.debug('JobServer connected.')
        else:
            rologger.debug('Running in serial.')
        rologger.debug('Check out the database connection.')
        try:
            conn = Connection(self.options, parent_logger=parent_logger)
        except Exception as e:
            rologger.error('Uncaught Exception: {} {}'.format(type(e), e))
            sys.exit(1)
        finally:
            del conn
        rologger.debug('Database connection established.')
        rologger.debug('Config sucessfully read in.')


class RinexArchive(object):

    def __init__(self, archiveoptions: ReadOptions):
        try:
            conn = Connection(archiveoptions.options)
            # Read the structure definition table
            self.levels = conn.load_tankstruct()
            self.keys = conn.load_table('keys')
            # Read the station and network tables
            self.networks = conn.load_table('networks')
            self.stations = conn.load_table('stations')
            del conn
        except Exception as e:
            print('Uncaught Exception: {} {}'.format(type(e), e))
            sys.exit(1)

    @staticmethod
    def parse_crinex_filename(filename):
        # parse a crinex filename
        try:
            sfile = re.findall(r'(\w{4})(\d{3})(\w{1})\.(\d{2})([d])\.[Z]$', filename)
            if sfile:
                return sfile[0]
            else:
                return None
        except Exception as e:
            print('Uncaught exception: {} {}'.format(type(e), e))
            sys.exit(1)

    @staticmethod
    def parse_rinex_filename(filename):
        # parse a rinex filename
        sfile = re.findall(r'(\w{4})(\d{3})(\w{1})\.(\d{2})([o])$', filename)

        if sfile:
            return sfile[0]
        else:
            return []

    def scan_archive_struct(self, rootdir: str) -> list:
        """
        Recursive member method of RinexArcvhive that searches through the given rootdir
        to find files matching a compressed rinex file e.g. ending with d.Z.  The method
        self.scan_archive_struct() is used to determine the file type.
        :param rootdir:
        :return:
        """
        try:
            file = []
            with os.scandir(rootdir) as it:
                for entry in it:
                    if os.path.isdir(entry.path):
                        file.extend(self.scan_archive_struct(entry.path))
                    # DDG issue #15: match the name of the file to a valid rinex filename
                    elif self.parse_crinex_filename(entry.name):
                        # only add valid rinex compressed files
                        file.append(entry)
            return file
        except Exception as e:
            print('Uncaught Exception: {} {}'.format(type(e), e))
            sys.exit(1)


class JobServer:

    def __init__(self, options, parent_logger='archive'):
        """
        Initialize the JobServer
        :param options: pyOptions.ReadOptions instance
        :param parent_logger: Name of the function creating a new instance of JobServer
        """
        self.head_logger = logging.getLogger('.'.join([parent_logger, 'gpys.JobServer']))
        self.options = options
        self.cluster = None
        self.tested = False
        self.cluster_test()
        if self.tested is False:
            raise ConnectionError('Connection failed in JobServer.__init__')

    def __del__(self):
        try:
            self.head_logger.debug('Attempting to shut down the cluster.')
            if isinstance(self.cluster, dispy.JobCluster):
                self.cluster.shutdown()
                self.head_logger.debug('Cluster successfully shut down.')
            else:
                self.head_logger.debug('Cluster was not started.')
        except Exception as e:
            self.head_logger.error('Uncaught Exception: {} {}'.format(type(e), e))
            sys.exit(1)

    def connect(self, compute, setup=None):
        try:
            self.head_logger.debug('Testing out the cluster.')
            self.cluster = dispy.JobCluster(compute,
                                            ip_addr=self.options['head_node'],
                                            setup=setup,
                                            ping_interval=int(self.options['ping_interval']),
                                            pulse_interval=6,
                                            loglevel=self.head_logger.getEffectiveLevel())
            for node in self.options['node_list']:
                self.cluster.discover_nodes(node)
                self.head_logger.debug('Waiting {} seconds.'.format(2*int(self.options['ping_interval'])))
                time.sleep(2*int(self.options['ping_interval']))
        except ConnectionError as e:
            self.head_logger.error(e, exc_info=sys.exc_info())
            sys.exit(1)
        except Exception as e:
            self.head_logger.error('Uncaught Exception: {} {}'.format(type(e), e))
            sys.exit(1)

    def cluster_test(self):
        j, tend, tstart = None, None, None
        try:
            self.head_logger.debug('Testing out the cluster.')

            def compute():
                time.sleep(0.1)
                return socket.gethostname()

            self.connect(compute)

            jobs = []
            for node in self.options['node_list']:
                self.head_logger.debug('Sending job to {}'.format(node))
                job = self.cluster.submit_job_id_node('InitialTest-{}'.format(node), node)
                jobs.append(job)
            if None in jobs:
                raise ConnectionError('Error while submitting job.  '
                                      'The server may not be '
                                      'started on {}.'.format(self.options['node_list'][jobs.index(None)]))
            tstart = time.time()
            while not self.cluster.wait():
                time.sleep(0.1)
            tend = time.time()
            self.head_logger.debug('Jobs took {:.4} seconds.'.format(tend-tstart))
            for j in jobs:
                self.head_logger.debug('Node worked! {}.'.format(j.result))
            for node in self.options['node_list']:
                self.head_logger.debug('Sending files to {}.'.format(node))
            self.head_logger.debug('Started a Dispy job and it worked :D')
            self.tested = True
        except ConnectionError as e:
            self.head_logger.error(e)
            sys.exit(1)
        except Exception as e:
            self.head_logger.error('Uncaught Exception: {} {}'.format(type(e), e))
            sys.exit(1)
        finally:
            self.head_logger.debug('Shutting down cluster.')
            if isinstance(self.cluster, dispy.JobCluster):
                self.cluster.shutdown()
            del j, tend, tstart

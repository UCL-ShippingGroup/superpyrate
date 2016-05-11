import luigi
from luigi import six, postgres
from luigi.util import inherits
from luigi.contrib.sge import SGEJobTask as SGEJobTask
from pyrate.algorithms.aisparser import readcsv, parse_raw_row, AIS_CSV_COLUMNS, validate_row
from pyrate.repositories.aisdb import AISdb
from superpyrate.tasks import produce_valid_csv_file
import csv
import datetime
import psycopg2
import logging
import tempfile
import os
logger = logging.getLogger('luigi-interface')

class Pipeline(luigi.WrapperTask):
    """Wrapper task which performs the entire ingest pipeline
    """
    # Pass in folder with CSV files to parse into Database when calling luigi from the command line: luigi --module superpyrate.pipeline Pipeline --aiscsv-folder ./aiscsv --local-scheduler --workers=2
    aiscsv_folder = luigi.Parameter()

    def requires(self):
        # yield [ValidMessagesToDatabase(in_file, os.path.abspath(self.aiscsv_folder)) for in_file in os.listdir(self.aiscsv_folder) if in_file.endswith('.csv')]
        yield [LoadCleanedAIS(in_file, os.path.abspath(self.aiscsv_folder)) for in_file in os.listdir(self.aiscsv_folder) if in_file.endswith('.csv')]

class SourceFiles(luigi.ExternalTask):

    in_file = luigi.Parameter()
    aiscsv_folder = luigi.Parameter()

    def output(self):
        return luigi.file.LocalTarget(self.aiscsv_folder + '/' + self.in_file)

    def output(self):
        base_path = os.path.join(self.source_path,
                                 'exactEarth_historical_data_%Y%m%d.csv')
        date_path = self.date.strftime(base_path)
        return luigi.file.LocalTarget(date_path)

@inherits(SourceFiles)
class ValidMessages(luigi.Task):
    """ Takes AIS messages and runs validation functions, generating valid csv
    files in folder called 'cleancsv' at the same level as aiscsv_folder
    """
    in_file = luigi.Parameter()
    aiscsv_folder = luigi.Parameter()

    def requires(self):
        return SourceFiles(self.in_file, self.aiscsv_folder)

    def work(self):
        produce_valid_csv_file(self.input(), self.output())

    def output(self):
        clean_file_out = os.path.dirname(self.aiscsv_folder) + '/cleancsv/' + self.in_file
        return luigi.file.LocalTarget(clean_file_out)

@inherits(ValidMessages)
class ValidMessagesToDatabase(luigi.postgres.CopyToTable):

    in_file = luigi.Parameter()
    aiscsv_folder = luigi.Parameter()

    null_values = (None,"")
    column_separator = ","

    host = "localhost"
    database = "test_aisdb"
    user = "postgres"
    password = ""
    table = "ais_clean"

    cols = ['MMSI','Time','Message_ID','Navigational_status','SOG',
               'Longitude','Latitude','COG','Heading','IMO','Draught',
               'Destination','Vessel_Name',
               'ETA_month','ETA_day','ETA_hour','ETA_minute']
    columns = [x.lower() for x in cols]
    # logger.debug("Columns: {}".format(columns))

    def rows(self):
        """
        Return/yield tuples or lists corresponding to each row to be inserted.
        """
        with self.input().open('r') as csvfile:
            reader = csv.reader(csvfile)
            for row in reader:
                yield row
                # logger.debug(line)
                # yield [x for x in line.strip('\n').split(',') ]

    def requires(self):
        return ValidMessages(self.in_file, self.aiscsv_folder)

    def copy(self, cursor, file):
        if isinstance(self.columns[0], six.string_types):
            column_names = self.columns
        elif len(self.columns[0]) == 2:
            column_names = [c[0] for c in self.columns]
        else:
            raise Exception('columns must consist of column strings or (column string, type string) tuples (was %r ...)' % (self.columns[0],))
        logger.debug(self.columns)
        sql = "COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)".format(self.table, ",".join(self.columns), file)

        cursor.copy_expert(sql, file)

    def run(self):
        """
        Inserts data generated by rows() into target table.

        If the target table doesn't exist, self.create_table will be called to attempt to create the table.

        Normally you don't want to override this.
        """
        if not (self.table and self.columns):
            raise Exception("table and columns need to be specified")

        connection = self.output().connect()

        with self.input().open('r') as csvfile:
            for attempt in range(2):
                try:
                    cursor = connection.cursor()
                    # self.init_copy(connection)
                    self.copy(cursor, csvfile)
                    # self.post_copy(connection)
                except psycopg2.ProgrammingError as e:
                    if e.pgcode == psycopg2.errorcodes.UNDEFINED_TABLE and attempt == 0:
                        # if first attempt fails with "relation not found", try creating table
                        logger.info("Creating table %s", self.table)
                        connection.reset()
                        self.create_table(connection)
                    else:
                        raise
                else:
                    break

        # mark as complete in same transaction
        self.output().touch(connection)

        # commit and clean up
        connection.commit()
        connection.close()

class LoadCleanedAIS(luigi.postgres.CopyToTable):
    """
    Execute ValidMessagesToDatabase and update ais_sources table with name of CSV processed
    """

    in_file = luigi.Parameter()
    aiscsv_folder = luigi.Parameter()

    null_values = (None,"")
    column_separator = ","

    host = "localhost"
    database = "test_aisdb"
    user = "postgres"
    password = ""
    table = "ais_sources"

    def requires(self):
        return ValidMessagesToDatabase(self.in_file, self.aiscsv_folder)

    def run(self):
        # Prepare source data to add to ais_sources
        source_data = {'filename':self.in_file,'ext':os.path.splitext(self.in_file)[1],'invalid':0,'clean':0,'dirty':0,'source':0}
        columns = '(' + ','.join([c.lower() for c in source_data.keys()]) + ')'

        connection = self.output().connect()
        cursor = connection.cursor()
        with cursor:
            tuplestr = "(" + ",".join("%({})s".format(i) for i in source_data.keys()) + ")"
            cursor.execute("INSERT INTO " + self.table + " "+ columns + " VALUES "+ tuplestr, source_data)

        # mark as complete
        self.output().touch(connection)

        # commit and clean up
        connection.commit()
        connection.close()

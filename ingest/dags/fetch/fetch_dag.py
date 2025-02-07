from airflow.decorators import dag, task
from airflow.operators.empty import EmptyOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from pendulum import datetime, duration, now

import requests
import os
import shutil 
import zipfile
import polars as pl
import pandas as pd

default_args = {
    "owner": "admin",
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 1,
    "retry_delay": duration(seconds=30),
}

@dag(
    dag_id="fetch",
    default_args=default_args,
    schedule_interval=None,  
    catchup=False,
    is_paused_upon_creation=False,
)
def ingest():

    keys = ['name', 'fec_code', 'cycle', 'run_date', 'extension', 'temp_dir']

    @task
    def process_config(**context): 
        dag_run = context['dag_run'].conf
        config = {key: dag_run.get(key) for key in keys}
        return config

    @task
    def initialize_paths(config):
        name = config['name']
        fec_code = config['fec_code']
        cycle = config['cycle']
        run_date = config['run_date']
        extension = config['extension']
        temp_dir = config['temp_dir']

        suffix = cycle[-2:]
        
        input_dir = f'{temp_dir}{name}_{cycle}/in/'
        output_dir = f'{temp_dir}{name}_{cycle}/out/'
        data_dir = input_dir + 'data/'
        cleaned_data_path = data_dir + 'cleaned_data.txt'
        output_name = f'{run_date}_{name}_{cycle}{extension}'

        paths = {
            'input_dir': input_dir,
            'output_dir': output_dir,
            'data_dir': data_dir,
            'cleaned_data_path': cleaned_data_path,
            'output_name': output_name,
            'cycle': cycle,
            'fec_code': fec_code,
            'suffix': suffix,
            'name': name
        }

        return paths

    @task
    def start():
        EmptyOperator(task_id="start")

    @task
    def create_dirs(paths):
        input_dir = paths['input_dir']
        output_dir = paths['output_dir']
        data_dir = paths['data_dir']

        for directory in [input_dir, output_dir, data_dir]:
            if os.path.exists(directory):
                shutil.rmtree(directory)
            os.makedirs(directory)

    @task
    def get_data(paths):
        name = paths['name']
        cycle = paths['cycle']
        fec_code = paths['fec_code']
        suffix = paths['suffix']
        data_dir = paths['data_dir']
        data_url = f'https://www.fec.gov/files/bulk-downloads/{cycle}/{fec_code}{suffix}.zip'
        
        zip_path = f'{data_dir}{name}_{cycle}.zip'  

        with open(zip_path, 'wb') as zipped:  
            zipped.write(requests.get(data_url).content)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(data_dir)

        os.remove(zip_path)
    
    @task
    def preprocess(paths):
        data_dir = paths['data_dir']
        cleaned_data_path = paths['cleaned_data_path']

        raw_data_path = os.path.join(data_dir, os.listdir(data_dir)[0])

        with open(raw_data_path, 'r', encoding='utf-8') as file:
            cleaned = file.read().replace(',|', '|').replace('"','').replace("'","")

        with open(cleaned_data_path, 'w', encoding='utf-8') as cleaned_data:
            cleaned_data.write(cleaned)
    
    @task
    def write(paths):
        cleaned_data_path = paths['cleaned_data_path']
        output_name = paths['output_name']
        output_dir = paths['output_dir']

        schema_df = pd.read_csv(f'/opt/airflow/schemas/{paths['name']}.csv')

        header = list(schema_df['attribute'])

        dtype = {name : pl.Utf8 for name in header}

        df = pl.read_csv(cleaned_data_path, separator='|', new_columns=header, 
                        schema_overrides=dtype, infer_schema_length=0, encoding="utf-8", ignore_errors=True)

        df.write_csv(os.path.join(output_dir, output_name), line_terminator='\n')

    trigger_staging_task = TriggerDagRunOperator(
        task_id='trigger_staging',
        trigger_dag_id='stage', 
        conf = {key: f'{{{{ ti.xcom_pull(task_ids="process_config")["{key}"] }}}}' for key in keys},
        wait_for_completion=False  
    )

    @task
    def stop():
        EmptyOperator(task_id="stop")

    config = process_config()
    paths = initialize_paths(config)

    start() >> create_dirs(paths) >> get_data(paths) >> preprocess(paths) >> write(paths) >> trigger_staging_task >> stop()


ingest()




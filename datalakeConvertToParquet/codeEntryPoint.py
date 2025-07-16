# ============================================= Importing Dependencies ============================================= #
from dataclasses import replace
import re
import json
import boto3
import urllib
import logging
import pandas as pd
from decouple import config
from datetime import datetime
from io import BytesIO, StringIO

from mypy_boto3_s3.client import Exceptions

# ============================================= Global Configurations ============================================= #
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger("boto3").setLevel(logging.ERROR)
logging.getLogger("botocore").setLevel(logging.ERROR)

INSTANCE_ID = None
SESSION = boto3.Session()
S3_CLIENT = SESSION.client('s3')
S3_RESOURCE = SESSION.resource('s3')
LAMBDA_CLIENT = boto3.client("lambda", region_name=config("REGION_NAME"))


def log_prefix(s3_directory):
    """
    Convert Bronze layer path to Silver layer path with year/month/date folder structure

    Input patterns:
    - "bronze/201_Expense_2025-01-10_2025-01-10_22_01.json"
    - "bronze/subfolder/201_Expense_2025-01-10_2025-01-10_22_01.json"

    Output:
    - "silver/year=2025/month=01/day=10/201_Expense_2025-01-10_2025-01-10_22_01.parquet"
    """
    try:
        key = str(s3_directory).replace("#", "Generic")

        # Split the key into parts
        parts = key.split('/')

        # Get the filename (last part)
        filename = parts[-1]
        file_base = filename.rsplit('.', 1)[0]  # Remove extension
        parquet_filename = f"{file_base}.parquet"

        # Extract date from filename pattern: ID_Type_YYYY-MM-DD_YYYY-MM-DD_HH_MM.extension
        # We'll use the first date (start date) for folder structure
        filename_parts = filename.split('_')

        year, month, day = None, None, None

        # Look for date pattern YYYY-MM-DD in filename parts
        for part in filename_parts:
            if len(part) == 10 and part.count('-') == 2:  # YYYY-MM-DD format
                try:
                    date_parts = part.split('-')
                    if len(date_parts) == 3 and len(date_parts[0]) == 4:
                        year = date_parts[0]
                        month = date_parts[1]
                        day = date_parts[2]
                        break  # Use the first valid date found
                except:
                    continue

        # If no date found in filename, try to extract from current date as fallback
        if not all([year, month, day]):
            from datetime import datetime
            current_date = datetime.now()
            year = str(current_date.year)
            month = f"{current_date.month:02d}"
            day = f"{current_date.day:02d}"
            logger.warning(f"Could not extract date from filename {filename}, using current date: {year}-{month}-{day}")

        # Construct the silver path with year/month/day structure
        silver_path = f"silver/year={year}/month={month}/day={day}/{parquet_filename}"

        return silver_path

    except Exception as e:
        logger.error(f"Error in getDirectoryToUpload: {str(e)}")
        # Fallback to simple conversion
        key = str(key).replace("#", "Generic")
        parts = key.split('/')
        if parts[0].lower() == 'bronze':
            parts[0] = 'silver'
        filename = parts[-1]
        file_base = filename.rsplit('.', 1)[0]
        parquet_filename = f"{file_base}.parquet"
        parts[-1] = parquet_filename
        return '/'.join(parts)


def send_email(subject="Data Proccessing Error(Bronze-Silver)", body="", bucket_name="", key=""):
    logger.info("Sending mail....")
    client = boto3.client("sns", region_name=config("REGION_NAME"))
    env_name = bucket_name.split('-')[1].upper()
    try:
        response = client.publish(
            TopicArn=config("TOPICARN"),
            Message=body,
            Subject=f"{env_name}: {subject}"
        )
    except Exception as e:
        logger.error(f"error occurred due to {str(e)}")
    else:
        logger.info("Email sent successfully")


def custom_validation(bucket_name, directory, df):
    try:
        datasource = (directory.split('source=')[1]).split('/')[0]
        if datasource.lower() == 'confluence_sftp':
            confluence_cols = []
            empty_cols = []
            for col in confluence_cols:
                if col in df.columns:
                    if not all(~df[col].isna()):
                        empty_cols.append(col)

            if empty_cols != []:
                message = f"Have found empty column values for columns : {empty_cols}"
                logger.warning(message)
                message = f"Hi,\n\nThe following are the error(s) occurred during the processing of data for the file {directory}.\n\n\n{message}\n\n\nThanks,\nSupport-Hexaview Team. \n\n----------------------This is system generated mail---------------------------"
                send_email(body=message, bucket_name=bucket_name, key=directory)
    except Exception as e:
        logger.error(f"Error in custom_validation: {str(e)}")
        pass


def upload_data(df, bucket_name, key, logPrefix):
    try:
        directory = getDirectoryToUpload(key)

        # removing the Existing handlers
        root = logging.getLogger()
        if root.handlers:
            for handler in root.handlers:
                root.removeHandler(handler)
        # setting up the basic formatter for the logger to use by deleting the previous configs
        logging.basicConfig(format=f"[%(levelname)s]\t[ {logPrefix} ]\t %(message)s")

        assert len(list(df.columns)) > 0, "Empty Dataframe, No Columns."
        assert len(df.index) > 0, "Empty Dataframe, No Rows."

        file_name = directory.split("/")[-1]
        data = StringIO(df.to_csv(index=False, index_label=0))
        df = pd.read_csv(data, dtype=object, na_filter=False)
        df = df.loc[:, ~df.columns.str.contains('Unnamed:', na=False)]

        # Validating Schema #
        df[df.columns] = df[df.columns].astype(str)  # converting every column to string #
        run_silver_crawler = False
        logger.info(f"Uploading file to : " + directory)
        df = df.loc[:, ~df.columns.str.contains('Unnamed:', na=False)]
        data = df.to_parquet(index=False)
        S3_RESOURCE.Object(bucket_name, directory).put(Body=data)

        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.basicConfig(format=f"[SUCCESS]\t[ {logPrefix} ]\t %(message)s")
        logger.info(f"upload successful")

        # try:
        #     payload = {"key": directory}
        #     response = LAMBDA_CLIENT.invoke(
        #         FunctionName=config("DUPLICATE_DATA_REMOVAL"),
        #         InvocationType='RequestResponse',
        #         Payload=json.dumps(payload)
        #     )
        #     response = json.load(response['Payload'])
        #     logger.info("de-duplication lambda triggered")
        # except:
        #     response = dict()

    except Exception as e:
        logger.error(str(e))
        logger.error("upload failed... terminating")
        logger.info("Sending Email")


# # Function to make S3 directory to upload the file (Updated for Bronze to Silver conversion)
def getDirectoryToUpload(key):
    """
    Convert Bronze layer path to Silver layer path with year/month/date folder structure based on today's date
    Input: bronze/generic/filename.csv or bronze/subfolder/filename.json
    Output: silver/year=2025/month=01/day=16/filename.parquet
    """
    try:
        key = str(key).replace("#", "Generic")

        # Get today's date
        today = datetime.now()
        year = str(today.year)
        month = f"{today.month:02d}"
        day = f"{today.day:02d}"

        # Split the key into parts
        parts = key.split('/')

        # Get the filename (last part)
        filename = parts[-1]
        file_base = filename.rsplit('.', 1)[0]  # Remove extension
        parquet_filename = f"{file_base}.parquet"

        # Construct the silver path with today's date structure
        silver_path = f"silver/year={year}/month={month}/day={day}/{parquet_filename}"

        logger.info(f"Converting {key} to {silver_path} using today's date: {year}-{month}-{day}")

        return silver_path

    except Exception as e:
        logger.error(f"Error in getDirectoryToUpload: {str(e)}")
        # Fallback to simple conversion
        key = str(key).replace("#", "Generic")
        parts = key.split('/')
        if parts[0].lower() == 'bronze':
            parts[0] = 'silver'
        filename = parts[-1]
        file_base = filename.rsplit('.', 1)[0]
        parquet_filename = f"{file_base}.parquet"
        parts[-1] = parquet_filename
        return '/'.join(parts)


def read_file_to_dataframe(data, filename, file_extension):
    """
    Read different file formats into pandas DataFrame
    """
    try:
        if file_extension.lower() == '.csv':
            df = pd.read_csv(data, dtype=str, na_filter=False)
        elif file_extension.lower() in ['.xlsx', '.xls']:
            df = pd.read_excel(data, dtype=str, na_filter=False)
        elif file_extension.lower() == '.json':
            df = pd.read_json(data, dtype=str)
        elif file_extension.lower() == '.parquet':
            df = pd.read_parquet(data)
            # Convert all columns to string for consistency
            df = df.astype(str)
        else:
            raise ValueError(f"Unsupported file format: {file_extension}")

        # Remove unnamed columns
        df = df.loc[:, ~df.columns.str.contains('Unnamed:', na=False)]

        return df
    except Exception as e:
        logger.error(f"Error reading file: {str(e)}")
        raise


# ============================================= Business logic ============================================= #
def dynamic_logic(bucket_name, key, logPrefix):
    try:
        # Download file from S3
        data = BytesIO()
        logger.info(f"Downloading file from: {key}")
        S3_CLIENT.download_fileobj(Bucket=bucket_name, Key=key, Fileobj=data)
        logger.info(f"File successfully downloaded.")
        data.seek(0)

        # Extract file information
        filename = key.split("/")[-1]
        file_extension = "." + filename.split(".")[-1]

        logger.info(f"Processing file: {filename} with extension: {file_extension}")

        # Read file into DataFrame based on file type
        df = read_file_to_dataframe(data, filename, file_extension)

        logger.info(f"DataFrame created with shape: {df.shape}")
        logger.info(f"Columns: {list(df.columns)}")

        # Perform custom validation if needed
        # custom_validation(bucket_name, key, df)

        # Upload processed data to Silver layer
        upload_data(df, bucket_name, key, logPrefix)

    except Exception as e:
        logger.error(f"Error in dynamic_logic: {str(e)}")
        raise


def lambda_handler(event, context):
    global INSTANCE_ID
    try:
        try:
            INSTANCE_ID = str(context.aws_request_id)
        except:
            INSTANCE_ID = 'No Instance ID'

        root = logging.getLogger()
        if root.handlers:
            for handler in root.handlers:
                root.removeHandler(handler)

        obj = event['Records'][0]['s3']['object']
        bucket_name = str(event['Records'][0]['s3']['bucket']['name'])
        key = str(obj['key'])
        key = urllib.parse.unquote_plus(key)

        # Validate that this is a Bronze layer file
        if not key.lower().startswith('bronze/'):
            logger.warning(f"File is not in Bronze layer, skipping: {key}")
            return {
                'statusCode': 200,
                'body': json.dumps('File not in Bronze layer, skipped processing')
            }

        # understand log prefix use case
        logPrefix = log_prefix(key)
        logging.basicConfig(format=f"[%(levelname)s]\t[ {logPrefix} ]\t %(message)s")
        logger.info("Started Bronze to Silver conversion..")
        logger.info(f"Processing file: {key}")

        dynamic_logic(bucket_name, key, logPrefix)

        return {
            'statusCode': 200,
            'body': json.dumps('Data successfully converted from Bronze to Silver!')
        }
    except Exception as e:
        logger.error(f"Error occurred: {e}")
        logger.error("Upload failed... terminating")
    return {
            'statusCode': 500,
            'body': json.dumps(f'Error processing file: {str(e)}')
        }


if __name__ == "__main__":
    # Test with sample Bronze layer files
    keys = [
        # "bronze/generic/year=2025/month=01/day=15/sample_data.csv",
        # "bronze/generic/year=2025/month=01/day=15/sample_data.xlsx",
        # "bronze/generic/year=2025/month=01/day=15/sample_data.json"
        "bronze/201_Expense_2025-01-10_2025-01-10_22_01.json"
    ]

    for key in keys:
        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {
                            "name": "zeropipelinedata"
                        },
                        "object": {
                            "key": key
                        }
                    }
                }
            ]
        }
        try:
            result = lambda_handler(event, None)
            print(f"Processed {key}: {result}")
        except Exception as e:
            print(f"Error processing {key}: {e}")

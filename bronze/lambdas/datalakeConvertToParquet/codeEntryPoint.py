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
from envision_logic import mapping_columns
from schema_monitoring import SchemaMonitoring
from plf_files_logic import PLF_DATA_PROCESSING, NegotiatedBasketProcessor, GenericBasketProcessor

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


# ============================================= Business Logic ============================================= #
def log_prefix(s3_directory):
    list_of_directory = s3_directory.split("/")
    STREAM = ""
    if (list_of_directory[1].split("=")[0] == "client" and list_of_directory[4].split("=")[0] == "client_code"):
        STREAM = "Conversion_To_Parquet" + " " + list_of_directory[2].split("=")[1] + " " + \
                 list_of_directory[1].split("=")[
                     1] + "_" + list_of_directory[4].split("=")[1] + " " + s3_directory.split("/")[3].split("=")[1]
    else:
        STREAM = "Conversion_To_Parquet" + " " + list_of_directory[2].split("=")[1] + " " + \
                 list_of_directory[1].split("=")[
                     1] + " " + s3_directory.split("/")[3].split("=")[1]
    return STREAM


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
    except:
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

        file_name = (directory.split('filename=')[1]).split('/')[0]
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


# Function to make S3 directory to upload the file (Updated Hierarchy PD-3434) #
def getDirectoryToUpload(key):
    key = str(key).replace("#", "Generic")
    filename = key.split('/')[-1]
    prefixEnd = key.find("/", key.find("filename="))
    prefix = key[:prefixEnd + 1]
    # print(prefix)
    midStart = key.find("year=")
    midEnd = key.find("/", key.find("date="))
    mid = key[midStart:midEnd + 1]
    # print(mid)
    sufix = key.replace(prefix, "").replace(mid, "").replace(filename, "")
    # print(sufix)
    parquetFilename = filename.replace(filename.split('.')[-1], "parquet")
    # print(parquetFilename)
    titaniumPrefix = prefix.replace("Bronze", "Silver")
    s3Key = f"{titaniumPrefix}{mid}{sufix}{parquetFilename}"
    # print(s3Key)
    return s3Key


# ============================================= Business logic ============================================= #
def dynamic_logic(bucket_name, key, logPrefix):
    data = BytesIO()
    logger.info(f"file downloading....")
    S3_CLIENT.download_fileobj(Bucket=bucket_name, Key=key, Fileobj=data)
    logger.info(f"file successfully downloaded.")
    data.seek(0)
    source = (key.split("/")[2]).split("=")[-1]
    filename = key.split("/")[-1]
    fileExtension = filename.split(".")[-1]
    fileExtension = "." + fileExtension
    filename = (key.split("/")[3]).split("=")[-1]
    if (fileExtension == ".csv"):
        # -------------------------------------- code for advisor SFTP fundBasketId -------------------------------------- #
        if filename.lower() == "advisor":
            basket_processor = None
            if 'basket_type=negotiated' in key.lower():
                basket_processor = NegotiatedBasketProcessor()
            else:
                basket_processor = GenericBasketProcessor()
            plf_data_processor = PLF_DATA_PROCESSING(key=key, data=data, basket_processor=basket_processor)
            directory_to_upload, df = plf_data_processor.process_raw_data()
            upload_data(df, bucket_name, directory_to_upload, logPrefix)
            return


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
        # understand log prefix use case
        logPrefix = log_prefix(key)
        logging.basicConfig(format=f"[%(levelname)s]\t[ {logPrefix} ]\t %(message)s")
        logger.info("Started..")
        dynamic_logic(bucket_name, key, logPrefix)
        return {
            'statusCode': 200,
            'body': json.dumps('Data Uploaded!')
        }
    except Exception as e:
        logger.error(f"error occured {e}")
        logger.error("upload failed... terminating")
        logger.info("Sending Email")
        message = f"Hi,\n\nThe following are the error(s) occurred during the processing of data.\n\n\n {e} \n\n\nThanks,\nSupport-Hexaview Team. \n\n----------------------This is system generated mail---------------------------"
        # send_email(body=message, bucket_name=bucket_name)


if __name__ == "__main__":
    keys = [
        "Bronze/client=TRUEMARK/source=ETFInvestOne_SFTP/filename=ETF_InvestOne_PLF/client_code=800/year=2025/month=5/date=29/ETF_3757edbaa4d5437e92130355b4950362_onDemand_800_QBUL_CXAI_SSB_PLF_20250529_140535_2025-05-29_2025-05-30_07:49.xlsx"
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
        lambda_handler(event, None)
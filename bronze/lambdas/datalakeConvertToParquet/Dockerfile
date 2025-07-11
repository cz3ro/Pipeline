FROM public.ecr.aws/lambda/python:3.9

COPY main_code/. ./

COPY . ./

RUN python3.9 -m pip install -r requirements.txt -t .

RUN rm requirements.txt
# Command can be overwritten by providing a different command in the template directly.
CMD ["codeEntryPoint.lambda_handler"]


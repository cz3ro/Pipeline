FROM public.ecr.aws/lambda/python:3.11

COPY . ./

RUN python3.11 -m pip install -r requirements.txt -t .

RUN rm requirements.txt
# Command can be overwritten by providing a different command in the template directly.
CMD ["codeEntryPoint.lambda_handler"]


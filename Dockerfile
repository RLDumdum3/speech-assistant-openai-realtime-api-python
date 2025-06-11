FROM python

WORKDIR /main

# Copy all files to the container
COPY . /main

# Install dependencies before running the app
RUN pip install --no-cache-dir -r requirements.txt

# Run your main Python script
CMD ["python3", "main.py"]

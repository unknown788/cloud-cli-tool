# Start from the official, lightweight Nginx web server image
FROM nginx:alpine

# Copy our generated dashboard into the default Nginx web root directory.
# Nginx will automatically serve this file on port 80.
COPY sample_app/index.html /usr/share/nginx/html/index.html




# # Start from an official Python base image
# FROM python:3.10-slim

# # Set the working directory inside the container
# WORKDIR /app

# # Copy the requirements file first to leverage Docker's layer caching
# COPY sample_app/requirements.txt .

# # Install the Python dependencies
# RUN pip install --no-cache-dir -r requirements.txt

# # Copy the rest of the application code into the container
# COPY sample_app/ .

# # Expose the port the app runs on
# EXPOSE 8000

# # The command to run when the container starts
# CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
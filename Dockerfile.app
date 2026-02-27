# This Dockerfile is used by the 'deploy' command.
# It serves the generated static dashboard via Nginx.
FROM nginx:alpine

# Remove the default Nginx welcome page
RUN rm -rf /usr/share/nginx/html/*

# Copy the generated web app into the Nginx serving directory
COPY sample_app/ /usr/share/nginx/html/

# Expose port 80 for HTTP traffic
EXPOSE 80

# Nginx starts automatically as the container's default command
CMD ["nginx", "-g", "daemon off;"]
import socket
import os
import time
import re
import select
import threading
import sys
import queue
from argparse import Namespace, ArgumentParser
from datetime import datetime
from urllib.parse import unquote, parse_qs


def parse_arguments() -> Namespace:
    """Parse command line arguments for the http server."""
    parser = ArgumentParser(
        prog="python -m http_server",
        description="HTTP Server with epoll for efficient I/O multiplexing",
        epilog="Authors: Your group name"
    )
    parser.add_argument("-a", "--address", type=str, default="0.0.0.0",
                        help="Set server address")
    parser.add_argument("-p", "--port", type=int, default=8000,
                        help="Set server port")
    parser.add_argument("-d", "--directory", type=str, default="public",
                        help="Set the directory to serve")
    return parser.parse_args()


# Configuration constants
BUFFER_SIZE = 4096
TIMEOUT = 10  # Seconds for persistent connection timeout

# MIME types mapping for content types
MIME_TYPES = {
    '.html': 'text/html',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.jpeg': 'image/jpeg',
    '.jpg': 'image/jpeg',
    '.png': 'image/png',
    '.pdf': 'application/pdf',
    '.txt': 'text/plain',
    '.ico': 'image/x-icon',
}

# Status codes and messages
STATUS_CODES = {
    200: 'OK',
    201: 'Created',
    400: 'Bad Request',
    404: 'Not Found',
}

# Dictionary to store user-specific data for personal cats
user_cats = {}
user_cats_lock = threading.Lock()  # Lock to protect shared resource


class HTTPRequest:
    """Class to handle HTTP request parsing and state management"""

    def __init__(self):
        self.method = None
        self.path = None
        self.http_version = None
        self.headers = {}
        self.body = None
        self.raw_data = b''
        self.is_complete = False
        self.content_length = None
        self.headers_parsed = False

    def process_data(self, data):
        """Process incoming data and update request state"""
        self.raw_data += data

        # Parse headers if not done yet
        if not self.headers_parsed and b'\r\n\r\n' in self.raw_data:
            self.parse_headers()

        # Check for complete body if headers are parsed
        if self.headers_parsed:
            headers_end = self.raw_data.find(b'\r\n\r\n') + 4
            body_received = len(self.raw_data) - headers_end

            # For POST requests with Content-Length
            if self.method == 'POST' and self.content_length is not None:
                if body_received >= self.content_length:
                    self.body = self.raw_data[headers_end:headers_end + self.content_length]
                    self.is_complete = True
            else:
                # For GET requests
                self.is_complete = True

        return self.is_complete

    def parse_headers(self):
        """Parse request headers"""
        # Find the end of headers
        headers_end = self.raw_data.find(b'\r\n\r\n')
        if headers_end == -1:
            return False

        headers_data = self.raw_data[:headers_end].decode('utf-8')
        lines = headers_data.split('\r\n')
        request_line = lines[0].split()

        if len(request_line) != 3:
            return False

        self.method = request_line[0]
        self.path = unquote(request_line[1])
        self.http_version = request_line[2]

        # Parse header lines
        for i in range(1, len(lines)):
            if ': ' in lines[i]:
                key, value = lines[i].split(': ', 1)
                self.headers[key.lower()] = value

        # Get content length for POST requests
        if self.method == 'POST':
            content_length_str = self.headers.get('content-length')
            if content_length_str:
                self.content_length = int(content_length_str)

        self.headers_parsed = True
        return True


class HTTPResponse:
    """Class to handle HTTP response creation and partial sending"""

    def __init__(self, status_code, content_type, content):
        self.status_code = status_code
        self.content_type = content_type
        self.content = content
        self.headers = []
        self.position = 0  # Position in response for partial sends
        self.headers_sent = False

    def prepare_headers(self):
        """Prepare the response headers"""
        # Convert content to bytes if it's a string
        if isinstance(self.content, str):
            self.content = self.content.encode('utf-8')

        self.headers = [
            f"HTTP/1.1 {self.status_code} {STATUS_CODES.get(self.status_code, 'Unknown')}",
            f"Date: {datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')}",
            "Server: PythonHTTP/1.1",
            f"Content-Type: {self.content_type}; charset=UTF-8",
            f"Content-Length: {len(self.content)}",
            "Connection: keep-alive"
        ]

        # Prepare the full response with headers
        self.response_headers = '\r\n'.join(self.headers) + '\r\n\r\n'
        self.response_headers_bytes = self.response_headers.encode('utf-8')

    def get_next_chunk(self, max_size=BUFFER_SIZE):
        """Get the next chunk of data to send"""
        if not self.headers_sent:
            # Send headers first
            chunk = self.response_headers_bytes
            self.headers_sent = True
            return chunk
        else:
            # Send a chunk of the content
            start = self.position
            end = min(self.position + max_size, len(self.content))
            chunk = self.content[start:end]
            self.position = end
            return chunk

    def is_complete(self):
        """Check if the entire response has been sent"""
        return self.headers_sent and self.position >= len(self.content)


class EPollHTTPServer:
    """HTTP server using epoll for efficient I/O multiplexing"""

    def __init__(self, host, port, server_root):
        self.host = host
        self.port = port
        self.server_root = server_root
        self.server_socket = None
        self.epoll = None
        self.connections = {}  # fd -> socket
        self.requests = {}  # fd -> HTTPRequest
        self.responses = {}  # fd -> HTTPResponse
        self.client_addr = {}  # fd -> (ip, port)
        self.last_activity = {}  # fd -> timestamp

    def start(self):
        """Start the HTTP server"""
        # Check if epoll is available
        if not hasattr(select, 'epoll'):
            print("Error: epoll is not available on this platform.")
            print("This server requires Linux for epoll functionality.")
            sys.exit(1)

        # Create socket
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.setblocking(0)  # Non-blocking socket
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(1000)  # Allow a large backlog for connections

        # Create epoll object
        self.epoll = select.epoll()
        self.epoll.register(self.server_socket.fileno(), select.EPOLLIN)

        print(f"HTTP Server running on http://{self.host}:{self.port}")
        print(f"Serving files from directory: {self.server_root}")

        # Start cleanup thread for inactive connections
        cleanup_thread = threading.Thread(target=self.cleanup_inactive_connections)
        cleanup_thread.daemon = True
        cleanup_thread.start()

        # Main event loop
        while True:
            events = self.epoll.poll(1)  # 1 second timeout

            for fileno, event in events:
                # New connection
                if fileno == self.server_socket.fileno():
                    self.accept_connection()
                # Existing connection has data to read
                elif event & select.EPOLLIN:
                    self.handle_read(fileno)
                # Socket is available for writing
                elif event & select.EPOLLOUT:
                    self.handle_write(fileno)
                # Connection closed or error
                elif event & (select.EPOLLHUP | select.EPOLLERR):
                    self.close_connection(fileno)

    def accept_connection(self):
        """Accept a new client connection"""
        client_socket, address = self.server_socket.accept()
        client_socket.setblocking(0)  # Non-blocking
        fileno = client_socket.fileno()

        # Register for read events
        self.epoll.register(fileno, select.EPOLLIN)

        # Store connection info
        self.connections[fileno] = client_socket
        self.requests[fileno] = HTTPRequest()
        self.client_addr[fileno] = address
        self.last_activity[fileno] = time.time()

        print(f"Connection accepted from {address[0]}:{address[1]}")

    def handle_read(self, fileno):
        """Handle data from a client connection"""
        self.last_activity[fileno] = time.time()
        data = self.connections[fileno].recv(BUFFER_SIZE)

        if data:
            # Process the received data
            request = self.requests[fileno]
            request_complete = request.process_data(data)

            if request_complete:
                # Generate response
                response = self.process_request(request, fileno)
                self.responses[fileno] = response
                self.epoll.modify(fileno, select.EPOLLOUT)
        else:
            # No data means client closed the connection
            self.close_connection(fileno)

    def handle_write(self, fileno):
        """Send data to a client"""
        self.last_activity[fileno] = time.time()
        response = self.responses[fileno]
        chunk = response.get_next_chunk()

        if chunk:
            # Send data
            bytes_sent = self.connections[fileno].send(chunk)
            # If we couldn't send all data, adjust the position
            if bytes_sent < len(chunk) and response.headers_sent:
                response.position -= (len(chunk) - bytes_sent)

        # Check if response is complete
        if response.is_complete():
            # Check if we need to keep the connection alive
            keep_alive = True
            request = self.requests[fileno]

            if request.http_version == 'HTTP/1.0':
                keep_alive = False
            else:
                connection_header = request.headers.get('connection', '').lower()
                keep_alive = connection_header != 'close'

            if keep_alive:
                # Reset for next request
                self.requests[fileno] = HTTPRequest()
                self.epoll.modify(fileno, select.EPOLLIN)
            else:
                # Close the connection
                self.close_connection(fileno)

    def close_connection(self, fileno):
        """Close a client connection"""
        if fileno not in self.connections:
            return

        # Unregister from epoll and close socket
        self.epoll.unregister(fileno)
        self.connections[fileno].close()

        # Remove from dictionaries
        for d in [self.connections, self.requests, self.responses, self.client_addr, self.last_activity]:
            if fileno in d:
                del d[fileno]

    def cleanup_inactive_connections(self):
        """Periodically clean up inactive connections"""
        while True:
            time.sleep(1)  # Check every second
            now = time.time()
            for fileno, last_time in list(self.last_activity.items()):
                if now - last_time > TIMEOUT:
                    self.close_connection(fileno)

    def process_request(self, request, fileno):
        """Process an HTTP request and generate a response"""
        client_id = f"{self.client_addr[fileno][0]}:{self.client_addr[fileno][1]}"

        # Check if request is valid
        if not request.headers_parsed or not request.method:
            return self.serve_error_page(400)

        if request.method == 'GET':
            return self.handle_get_request(request, client_id)
        elif request.method == 'POST':
            return self.handle_post_request(request, client_id)
        else:
            return self.serve_error_page(400)

    def serve_error_page(self, status_code):
        """Serve an error page from the appropriate HTML file"""
        if status_code == 400:
            file_path = os.path.join(self.server_root, '400.html')
        elif status_code == 404:
            file_path = os.path.join(self.server_root, '404.html')
        else:
            # Fallback to creating a basic error response
            return self.create_response(status_code, 'text/html',
                                        f"<html><body><h1>{status_code} {STATUS_CODES.get(status_code, 'Error')}</h1></body></html>")

        # Check if the error page exists
        if os.path.isfile(file_path):
            with open(file_path, 'r') as f:
                content = f.read()
            return self.create_response(status_code, 'text/html', content)

        # Fallback if specific error page doesn't exist
        return self.create_response(status_code, 'text/html',
                                    f"<html><body><h1>{status_code} {STATUS_CODES.get(status_code, 'Error')}</h1></body></html>")

    def handle_get_request(self, request, client_id):
        """Handle GET requests"""
        path = request.path.rstrip('/')  # Remove trailing slash if present

        # Default to index.html if root path
        if path == '' or path == '/':
            path = '/index.html'

        # Check if it's the personal cats page
        if path == '/personal_cats.html':
            return self.serve_personal_cats(client_id)

        # Map URL path to file path
        file_path = os.path.join(self.server_root, path.lstrip('/'))

        # Security check: prevent directory traversal
        real_path = os.path.realpath(file_path)
        server_path = os.path.realpath(self.server_root)
        if not real_path.startswith(server_path):
            return self.serve_error_page(400)

        # Check if file exists
        if not os.path.isfile(file_path):
            # Try with /index.html appended for directories
            if os.path.isdir(file_path):
                file_path = os.path.join(file_path, 'index.html')
                if os.path.isfile(file_path):
                    # Get content type and read file
                    _, ext = os.path.splitext(file_path)
                    content_type = MIME_TYPES.get(ext.lower(), 'application/octet-stream')
                    content = self.read_file(file_path)
                    return self.create_response(200, content_type, content)
            return self.serve_error_page(404)

        # Get content type and read file
        _, ext = os.path.splitext(file_path)
        content_type = MIME_TYPES.get(ext.lower(), 'application/octet-stream')
        content = self.read_file(file_path)

        # Create response
        return self.create_response(200, content_type, content)

    def handle_post_request(self, request, client_id):
        """Handle POST requests"""
        if request.path == '/data':
            # Parse form data from request body
            content_type = request.headers.get('content-type', '')

            if 'application/x-www-form-urlencoded' in content_type:
                # Decode the form data
                body_str = request.body.decode('utf-8')
                parsed_data = parse_qs(body_str)

                # Extract form fields
                cat_url = parsed_data.get('cat_url', [''])[0]
                description = parsed_data.get('description', [''])[0]

                # Validate form fields
                if not cat_url or not description:
                    return self.serve_error_page(400)

                # Store the cat data for this client
                with user_cats_lock:
                    if client_id not in user_cats:
                        user_cats[client_id] = []
                    user_cats[client_id].append({
                        'url': cat_url,
                        'description': description
                    })

                # Redirect to success page
                success_content = self.read_file(os.path.join(self.server_root, 'success.html'))
                return self.create_response(201, 'text/html', success_content)
            else:
                return self.serve_error_page(400)
        else:
            return self.serve_error_page(400)

    def serve_personal_cats(self, client_id):
        """Serve the personal cats page"""
        # Read personal_cats.html template
        file_path = os.path.join(self.server_root, 'personal_cats.html')

        try:
            with open(file_path, 'r') as f:
                template = f.read()
        except FileNotFoundError:
            return self.serve_error_page(404)

        # Build cats HTML
        cats_html = ""
        with user_cats_lock:
            if client_id in user_cats:
                for cat in user_cats[client_id]:
                    cats_html += f'''
                    <div class="cat-entry">
                        <img src="{cat["url"]}" alt="Cat Image">
                        <p>{cat["description"]}</p>
                    </div>
                    '''

        # Replace placeholder with actual cats
        content = template.replace('{{CATS_PLACEHOLDER}}', cats_html)
        return self.create_response(200, 'text/html', content)

    def create_response(self, status_code, content_type, content):
        """Create an HTTP response object"""
        response = HTTPResponse(status_code, content_type, content)
        response.prepare_headers()
        return response

    def read_file(self, file_path):
        """Read a file from disk"""
        # Security check: prevent directory traversal
        real_path = os.path.realpath(file_path)
        server_path = os.path.realpath(self.server_root)
        if not real_path.startswith(server_path):
            raise ValueError("Bad request")

        # Determine if binary or text mode needed
        _, ext = os.path.splitext(file_path)
        is_binary = ext.lower() not in ['.html', '.css', '.js', '.txt']
        mode = 'rb' if is_binary else 'r'

        with open(file_path, mode) as file:
            return file.read()

    def cleanup(self):
        """Clean up resources"""
        if self.epoll:
            self.epoll.unregister(self.server_socket.fileno())
            self.epoll.close()

        if self.server_socket:
            self.server_socket.close()


class SimpleThreadPoolHTTPServer:
    """Simple thread pool HTTP server for platforms without epoll"""

    def __init__(self, host, port, server_root):
        self.host = host
        self.port = port
        self.server_root = server_root
        self.running = True

    def start(self):
        print("Thread pool HTTP server not implemented")
        print("This server requires Linux with epoll support")
        sys.exit(1)


def main() -> None:
    """Main function to start the HTTP server"""
    parser = parse_arguments()
    port = parser.port
    host = parser.address
    base_directory = parser.directory

    # Check if the directory exists
    if not os.path.exists(base_directory):
        print(f"Error: Directory '{base_directory}' does not exist.")
        sys.exit(1)

    # Check if epoll is available (Linux systems)
    if hasattr(select, 'epoll'):
        server = EPollHTTPServer(host, port, base_directory)
    else:
        server = SimpleThreadPoolHTTPServer(host, port, base_directory)

    # Start the server
    server.start()


if __name__ == "__main__":
    main()

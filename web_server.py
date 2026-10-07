#!/usr/bin/env python3
"""
Stock Analysis System - Web Server
A simple web server to serve reports and handle refresh requests.
Run this server, then open the HTML report in your browser.
Click "Refresh Report" to regenerate the analysis.
"""

import http.server
import socketserver
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime
from urllib.parse import urlparse, parse_qs

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PORT = 8765
REPORT_DIR = "Stock Market Daily Report"

# Global progress tracking
refresh_progress = {
    'status': 'idle',
    'step': 0,
    'total_steps': 11,
    'current_step': 'Ready',
    'start_time': None,
    'estimated_time': 420  # 7 minutes estimated
}


class StockAnalysisHandler(http.server.SimpleHTTPRequestHandler):
    """Custom HTTP handler for stock analysis server"""

    def __init__(self, *args, **kwargs):
        # Set the directory to serve files from
        super().__init__(*args, directory='.', **kwargs)

    def end_headers(self):
        # Add CORS headers for local development
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        """Handle CORS preflight requests"""
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        """Handle GET requests"""
        parsed_path = urlparse(self.path)

        if parsed_path.path == '/':
            # Redirect to latest report
            self.redirect_to_latest_report()
        elif parsed_path.path == '/status':
            # Return server status
            self.send_json_response({
                'status': 'running',
                'server_time': datetime.now().isoformat(),
                'port': PORT
            })
        elif parsed_path.path == '/progress':
            # Return refresh progress from file (more reliable than in-memory)
            try:
                if os.path.exists('.progress.json'):
                    with open('.progress.json', 'r') as f:
                        progress = json.load(f)
                    self.send_json_response(progress)
                else:
                    self.send_json_response(refresh_progress)
            except Exception as e:
                self.send_json_response(refresh_progress)
        elif parsed_path.path == '/reports':
            # List available reports
            self.list_reports()
        else:
            # Serve static files
            super().do_GET()

    def do_POST(self):
        """Handle POST requests"""
        parsed_path = urlparse(self.path)

        if parsed_path.path == '/refresh':
            # Trigger report refresh
            self.handle_refresh()
        elif parsed_path.path == '/set_language':
            # Handle language change - FAST translation instead of regeneration
            try:
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                data = json.loads(post_data.decode('utf-8'))
                new_lang = data.get('language', 'en')

                if new_lang == 'zh':
                    # Translate to Chinese (fast ~30 seconds)
                    def translate_to_chinese():
                        try:
                            print(f"\n[INFO] Translating report to Chinese...")

                            # Find latest English report
                            reports = self.get_reports_list()
                            if not reports:
                                print("[ERROR] No reports found to translate")
                                return False

                            latest_report = reports[0]['filename']
                            english_report_path = f"{REPORT_DIR}/{latest_report}"

                            # Translate using report generator
                            from modules.report_generator import ReportGenerator
                            report_gen = ReportGenerator()
                            chinese_report_path = report_gen.translate_report_to_chinese(english_report_path)

                            if chinese_report_path:
                                print(f"\n[OK] Report translated to Chinese: {chinese_report_path}")
                                return True
                            else:
                                print(f"\n[ERROR] Translation failed")
                                return False
                        except Exception as e:
                            print(f"\n[ERROR] Translation error: {e}")
                            import traceback
                            traceback.print_exc()
                            return False

                    # Run translation in background
                    thread = threading.Thread(target=translate_to_chinese)
                    thread.daemon = True
                    thread.start()

                    # Wait a moment for translation to start
                    import time
                    time.sleep(0.5)

                    self.send_json_response({
                        'success': True,
                        'message': 'Translating to Chinese... (~30 seconds)',
                        'redirect_url': f'/{REPORT_DIR}/daily_report_{datetime.now().strftime("%Y%m%d")}_zh.html'
                    })
                else:
                    # Switch back to English (just redirect, no work needed)
                    self.send_json_response({
                        'success': True,
                        'message': 'Switching to English...',
                        'redirect_url': f'/{REPORT_DIR}/daily_report_{datetime.now().strftime("%Y%m%d")}.html'
                    })

            except Exception as e:
                self.send_json_response({'success': False, 'error': str(e)}, status=500)
        else:
            self.send_error(404, "Not Found")

    def redirect_to_latest_report(self):
        """Redirect to the latest report"""
        try:
            reports = self.get_reports_list()
            if reports:
                latest = reports[0]['filename']
                self.send_response(302)
                self.send_header('Location', f'/{REPORT_DIR}/{latest}')
                self.end_headers()
            else:
                self.send_response(302)
                self.send_header('Location', '/no_report.html')
                self.end_headers()
        except Exception as e:
            self.send_error(500, str(e))

    def get_reports_list(self):
        """Get list of available reports sorted by date"""
        reports = []
        if os.path.exists(REPORT_DIR):
            for f in os.listdir(REPORT_DIR):
                if f.startswith('daily_report_') and f.endswith('.html'):
                    filepath = os.path.join(REPORT_DIR, f)
                    mtime = os.path.getmtime(filepath)
                    reports.append({
                        'filename': f,
                        'date': f.replace('daily_report_', '').replace('.html', ''),
                        'modified': datetime.fromtimestamp(mtime).isoformat()
                    })
        reports.sort(key=lambda x: x['date'], reverse=True)
        return reports

    def list_reports(self):
        """Return list of available reports"""
        try:
            reports = self.get_reports_list()
            self.send_json_response({'reports': reports})
        except Exception as e:
            self.send_json_response({'error': str(e)}, status=500)

    def handle_refresh(self):
        """Handle report refresh request"""
        global refresh_progress

        try:
            # Check if already running (check both in-memory and file)
            if refresh_progress['status'] == 'running':
                self.send_json_response({
                    'success': False,
                    'error': 'Analysis already in progress',
                    'progress': refresh_progress
                })
                return

            # Reset progress in memory
            refresh_progress['status'] = 'running'
            refresh_progress['step'] = 0
            refresh_progress['current_step'] = 'Initializing modules and connecting to IBKR...'
            refresh_progress['start_time'] = datetime.now().isoformat()

            # Also write to progress file immediately so HTML can see it
            try:
                with open('.progress.json', 'w') as f:
                    json.dump(refresh_progress, f)
            except Exception as e:
                print(f"Warning: Could not write progress file: {e}")

            # Run analysis in background thread
            def run_analysis():
                global refresh_progress
                try:
                    import main
                    main.main()
                    refresh_progress['status'] = 'completed'
                    refresh_progress['current_step'] = 'Analysis complete!'
                    refresh_progress['step'] = refresh_progress['total_steps']
                    print("\n[OK] Report refreshed successfully!")
                except Exception as e:
                    refresh_progress['status'] = 'error'
                    refresh_progress['current_step'] = f'Error: {str(e)}'
                    print(f"\n[ERROR] Failed to refresh report: {e}")

            thread = threading.Thread(target=run_analysis)
            thread.daemon = True
            thread.start()

            # Return immediately with progress info
            self.send_json_response({
                'success': True,
                'message': 'Analysis started in background',
                'progress': refresh_progress,
                'estimated_time': refresh_progress['estimated_time']
            })

        except Exception as e:
            refresh_progress['status'] = 'error'
            refresh_progress['current_step'] = f'Error: {str(e)}'
            self.send_json_response({
                'success': False,
                'error': str(e)
            }, status=500)

    def send_json_response(self, data, status=200):
        """Send JSON response"""
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        """Custom log format"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        print(f"[{timestamp}] {args[0]}")


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Allow reuse of address and handle multiple connections"""
    allow_reuse_address = True


def create_no_report_page():
    """Create a placeholder page when no reports exist"""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>No Reports Available</title>
    <style>
        body {
            font-family: 'Segoe UI', sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            background: #f8f9fa;
        }
        .container {
            text-align: center;
            padding: 40px;
            background: white;
            border-radius: 10px;
            box-shadow: 0 0 20px rgba(0,0,0,0.1);
        }
        h1 { color: #007bff; }
        .btn {
            background: #007bff;
            color: white;
            border: none;
            padding: 15px 30px;
            border-radius: 25px;
            cursor: pointer;
            font-size: 16px;
            margin-top: 20px;
        }
        .btn:hover { background: #0056b3; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 No Reports Available</h1>
        <p>Click the button below to generate your first analysis report.</p>
        <button class="btn" onclick="generateReport()">Generate Report</button>
        <p id="status" style="margin-top: 20px; color: #666;"></p>
    </div>
    <script>
        async function generateReport() {
            document.getElementById('status').textContent = 'Generating report... This may take a few minutes.';
            try {
                const response = await fetch('/refresh', { method: 'POST' });
                const data = await response.json();
                if (data.success) {
                    document.getElementById('status').textContent = 'Report generated! Redirecting...';
                    setTimeout(() => { window.location.href = '/'; }, 1500);
                } else {
                    document.getElementById('status').textContent = 'Error: ' + data.error;
                }
            } catch (e) {
                document.getElementById('status').textContent = 'Error: ' + e.message;
            }
        }
    </script>
</body>
</html>
    """
    with open('no_report.html', 'w') as f:
        f.write(html)


def main():
    """Main entry point"""
    print("=" * 60)
    print("  Stock Analysis System - Web Server")
    print("=" * 60)

    # Create placeholder page
    create_no_report_page()

    # Ensure report directory exists
    os.makedirs(REPORT_DIR, exist_ok=True)

    try:
        with ThreadedTCPServer(("", PORT), StockAnalysisHandler) as httpd:
            print(f"\n✓ Server running at http://localhost:{PORT}")
            print(f"✓ Reports directory: {REPORT_DIR}/")
            print("\nAvailable endpoints:")
            print(f"  • http://localhost:{PORT}/          - Latest report")
            print(f"  • http://localhost:{PORT}/reports   - List all reports")
            print(f"  • http://localhost:{PORT}/refresh   - Generate new report (POST)")
            print(f"  • http://localhost:{PORT}/status    - Server status")
            print("\nPress Ctrl+C to stop the server.")
            print("-" * 60)

            # Open browser to the report
            webbrowser.open(f'http://localhost:{PORT}')

            # Serve forever
            httpd.serve_forever()

    except KeyboardInterrupt:
        print("\n\nServer stopped.")
    except OSError as e:
        if e.errno == 48:  # Address already in use
            print(f"\n[ERROR] Port {PORT} is already in use.")
            print(f"Try: lsof -i :{PORT} | grep LISTEN")
            print("Then kill the process or use a different port.")
        else:
            raise


if __name__ == "__main__":
    main()

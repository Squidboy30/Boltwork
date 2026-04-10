from flask import Flask, jsonify
app = Flask(__name__)

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

@app.route('/test')
def test():
    return jsonify({"message": "402 would go here", "status": 402})

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)

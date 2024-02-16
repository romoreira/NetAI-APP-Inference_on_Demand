import aiohttp
import asyncio
from flask import Flask, request, jsonify

app = Flask(__name__)

async def fetch(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.text()

async def process_inference_request(urls):
    tasks = [fetch(url) for url in urls]
    results = await asyncio.gather(*tasks)
    return results

@app.route('/inference', methods=['GET'])
def process_text():
    try:
        urls = ['http://example.com', 'http://example.org', 'http://example.net']
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(process_inference_request(urls))
        print("Results size: "+str(len(results)))
        return jsonify(results), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', debug=True)
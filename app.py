from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import random, os, urllib.request, urllib.parse, json, xml.etree.ElementTree as ET
import http.client

app = Flask(__name__, static_folder='static')
CORS(app)

WOLFRAM_APPID = 'LJYQJ5XPV7'
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ────────────────────────────────────────────────────────────────
#  Claude (Anthropic) Proxy Route
# ────────────────────────────────────────────────────────────────

@app.route('/api/claude', methods=['POST'])
def claude_proxy():
    """Proxy to Anthropic API to keep API key server-side."""
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'No API key configured', 'content': [{'type':'text','text':'API key not set on server.'}]}), 500

    try:
        body = request.get_json()
        payload = json.dumps({
            'model': body.get('model', 'claude-sonnet-4-20250514'),
            'max_tokens': body.get('max_tokens', 1024),
            'system': body.get('system', ''),
            'messages': body.get('messages', []),
        }).encode('utf-8')

        conn = http.client.HTTPSConnection('api.anthropic.com', timeout=30)
        conn.request('POST', '/v1/messages', body=payload, headers={
            'Content-Type': 'application/json',
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
        })
        resp = conn.getresponse()
        data = resp.read().decode('utf-8')
        conn.close()
        return app.response_class(response=data, status=resp.status, mimetype='application/json')
    except Exception as e:
        return jsonify({'error': str(e), 'content': [{'type':'text','text':f'Server error: {e}'}]}), 500

# ────────────────────────────────────────────────────────────────
#  WolframAlpha Proxy Routes
# ────────────────────────────────────────────────────────────────

def wolfram_query(query: str, full: bool = False) -> dict:
    """Call WolframAlpha API and parse the XML response."""
    try:
        encoded_q = urllib.parse.quote(query)
        if full:
            url = (f'http://api.wolframalpha.com/v2/query'
                   f'?appid={WOLFRAM_APPID}&input={encoded_q}'
                   f'&format=plaintext,image&podstate=Step-by-step+solution'
                   f'&output=xml')
        else:
            url = (f'http://api.wolframalpha.com/v2/query'
                   f'?appid={WOLFRAM_APPID}&input={encoded_q}'
                   f'&format=plaintext&output=xml')

        req = urllib.request.Request(url, headers={'User-Agent': 'LearnWorldEduApp/1.0'})
        with urllib.request.urlopen(req, timeout=6) as resp:
            xml_data = resp.read().decode('utf-8')

        root = ET.fromstring(xml_data)
        success = root.get('success', 'false') == 'true'
        if not success:
            return {'answer': None, 'steps': [], 'image': None, 'raw': 'No result'}

        answer = None
        steps = []
        image_url = None

        for pod in root.findall('pod'):
            pod_id    = pod.get('id', '')
            pod_title = pod.get('title', '')

            for sub in pod.findall('subpod'):
                pt = sub.find('plaintext')
                img = sub.find('img')

                text = pt.text.strip() if pt is not None and pt.text else ''
                img_src = img.get('src', '') if img is not None else ''

                # Primary answer
                if pod_id in ('Result', 'DecimalApproximation', 'Solution',
                              'Derivative', 'IndefiniteIntegral') and text and not answer:
                    answer = text

                # Input interpretation (fallback answer)
                if pod_id == 'Input' and not answer and text:
                    answer = text

                # Step-by-step
                if 'step' in pod_title.lower() or 'step' in pod_id.lower():
                    if text:
                        steps.extend([s for s in text.split('\n') if s.strip()])
                    if img_src and not image_url:
                        image_url = img_src

                # Image for any result pod
                if img_src and not image_url and pod_id not in ('Input',):
                    image_url = img_src

        # If no clear answer, grab the first non-empty plaintext
        if not answer:
            for pod in root.findall('pod'):
                for sub in pod.findall('subpod'):
                    pt = sub.find('plaintext')
                    if pt is not None and pt.text and pt.text.strip():
                        answer = pt.text.strip()
                        break
                if answer:
                    break

        return {'answer': answer, 'steps': steps[:6], 'image': image_url, 'raw': answer}

    except Exception as e:
        return {'answer': None, 'steps': [], 'image': None, 'raw': str(e), 'error': True}


@app.route('/api/wolfram')
def wolfram_simple():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'answer': None, 'error': 'No query'}), 400
    return jsonify(wolfram_query(q, full=False))


@app.route('/api/wolfram/full')
def wolfram_full():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'answer': None, 'error': 'No query'}), 400
    return jsonify(wolfram_query(q, full=True))


# ────────────────────────────────────────────────────────────────
#  Math Problem Generation
# ────────────────────────────────────────────────────────────────

GRADE_CONFIG = {
    "1-1": {"name":"Grade 1 · Easy",   "desc":"Add/Sub ≤10",         "types":["add_small","sub_small"],             "goal":5},
    "1-2": {"name":"Grade 1 · Medium", "desc":"Add/Sub ≤20",         "types":["add_teen","sub_teen"],               "goal":6},
    "1-3": {"name":"Grade 1 · Hard",   "desc":"Carrying & Borrowing", "types":["add_carry","sub_borrow"],            "goal":7},
    "2-1": {"name":"Grade 2 · Easy",   "desc":"Multiplication 2–5×", "types":["mul_easy"],                          "goal":6},
    "2-2": {"name":"Grade 2 · Medium", "desc":"Mixed Ops ≤100",      "types":["add_100","sub_100","mul_easy"],       "goal":7},
    "2-3": {"name":"Grade 2 · Hard",   "desc":"3-Digit Add/Sub",     "types":["add_3digit","sub_3digit"],            "goal":8},
    "3-1": {"name":"Grade 3 · Easy",   "desc":"Times Tables + Div",  "types":["mul_full","div_easy"],                "goal":7},
    "3-2": {"name":"Grade 3 · Medium", "desc":"2-Digit × 1-Digit",  "types":["mul_2x1","div_full"],                 "goal":8},
    "3-3": {"name":"Grade 3 · Hard",   "desc":"2-Digit × 2-Digit",  "types":["mul_2x2","div_full","add_3digit"],    "goal":10},
}

def r(lo, hi): return random.randint(lo, hi)

def _make(op, a, b):
    if op == "+":
        if a is None: a = r(1,50)
        if b is None: b = r(1,50)
        return {"display":f"{a} + {b} = ?","answer":a+b,"op":"+","a":a,"b":b,
                "hint":f"Add {b} to {a}!"}
    if op == "-":
        if b is None: b = r(1, max(1, a-1))
        return {"display":f"{a} - {b} = ?","answer":a-b,"op":"-","a":a,"b":b,
                "hint":f"Subtract {b} from {a}!"}
    if op == "*":
        if a is None: a = r(2,9)
        if b is None: b = r(2,9)
        parts = "+".join([str(a)]*min(b,4)) + ("..." if b>4 else "")
        return {"display":f"{a} × {b} = ?","answer":a*b,"op":"*","a":a,"b":b,
                "hint":f"Add {a} a total of {b} times! ({parts})"}
    if op == "/":
        if b is None: b = r(2,9)
        ans = r(1,9); a = b*ans
        return {"display":f"{a} ÷ {b} = ?","answer":ans,"op":"/","a":a,"b":b,
                "hint":f"How many times does {b} go into {a}?"}

GENERATORS = {
    "add_small":  lambda: _make("+", r(1,9),    r(1,9)),
    "sub_small":  lambda: _make("-", r(2,10),   None),
    "add_teen":   lambda: _make("+", r(1,19),   r(1,19)),
    "sub_teen":   lambda: _make("-", r(5,20),   None),
    "add_carry":  lambda: _make("+", r(6,19),   r(6,19)),
    "sub_borrow": lambda: _make("-", r(11,30),  None),
    "add_100":    lambda: _make("+", r(10,90),  r(10,90)),
    "sub_100":    lambda: _make("-", r(20,99),  None),
    "mul_easy":   lambda: _make("*", r(2,5),    r(2,9)),
    "add_3digit": lambda: _make("+", r(100,899),r(10,99)),
    "sub_3digit": lambda: _make("-", r(200,999),None),
    "mul_full":   lambda: _make("*", r(2,9),    r(2,9)),
    "div_easy":   lambda: _make("/", None,      r(2,5)),
    "mul_2x1":    lambda: _make("*", r(10,99),  r(2,9)),
    "div_full":   lambda: _make("/", None,      r(2,9)),
    "mul_2x2":    lambda: _make("*", r(10,30),  r(10,20)),
}

def generate_problem(grade: int, diff: int) -> dict:
    key = f"{grade}-{diff}"
    cfg = GRADE_CONFIG.get(key, GRADE_CONFIG["1-1"])
    ptype = random.choice(cfg["types"])
    prob = GENERATORS[ptype]()
    prob.update({"grade":grade,"diff":diff,"grade_key":key,
                 "grade_name":cfg["name"],"grade_goal":cfg["goal"]})

    # Optionally verify/enrich with Wolfram
    wf = wolfram_query(prob["display"].replace(" = ?",""))
    if wf.get("answer") and not wf.get("error"):
        prob["wolfram_verified"] = True
        prob["wolfram_answer"] = wf["answer"]

    return prob

# ────────────────────────────────────────────────────────────────
#  Existing API routes
# ────────────────────────────────────────────────────────────────

@app.route('/api/problem')
def get_problem():
    grade = max(1, min(3, int(request.args.get("grade", 1))))
    diff  = max(1, min(3, int(request.args.get("diff",  1))))
    return jsonify(generate_problem(grade, diff))

@app.route('/api/check', methods=['POST'])
def check_answer():
    d = request.get_json()
    user = int(d.get("answer",0)); correct = int(d.get("correct",-1))
    hint  = bool(d.get("hint_used", False))
    ok = user == correct
    return jsonify({"correct":ok,"points":(5 if hint else 10) if ok else 0,
                    "direction":"down" if user>correct else "up",
                    "message":"Correct! 🎉" if ok else f"Wrong! 😢 The answer is {correct}!"})

@app.route('/api/grades')
def get_grades():
    return jsonify(GRADE_CONFIG)

# ────────────────────────────────────────────────────────────────
#  Static file serving
# ────────────────────────────────────────────────────────────────

@app.route("/", defaults={"path":""})
@app.route("/<path:path>")
def serve(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)

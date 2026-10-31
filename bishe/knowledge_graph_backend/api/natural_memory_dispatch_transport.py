"""One official Flash SDK request in a killable child; never a fallback/retry."""
import json,os,subprocess,sys
from pathlib import Path

MODEL='deepseek-flash'
ENDPOINT='https://api.deepseek.com'

def request_once(messages,*,max_tokens,timeout):
    # No shell, credentials or configurable remote endpoints in arguments.
    child=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve())],
        input=json.dumps({'messages':messages,'max_tokens':max_tokens,'timeout':timeout},ensure_ascii=False),
        capture_output=True,text=True,encoding='utf-8',timeout=timeout)
    if child.returncode:raise RuntimeError('dispatch_child_failed')
    return json.loads(child.stdout)

def sdk_response(incoming,*,factory=None):
    offline_factory = factory is not None
    if factory is None:
        from openai import OpenAI
        factory=OpenAI
    key=os.environ.get('DEEPSEEK_API_KEY')
    if not key:
        from dotenv import dotenv_values
        key=dotenv_values(Path(__file__).resolve().parents[1]/'.env').get('DEEPSEEK_API_KEY')
    if not key:raise RuntimeError('credential_missing')
    if not offline_factory and key in {'offline-placeholder','synthetic-not-real'}:
        raise RuntimeError('placeholder_credential_rejected')
    client=factory(api_key=key,base_url=ENDPOINT,max_retries=0)
    try:
        response=client.chat.completions.create(model=MODEL,messages=incoming['messages'],temperature=0,
            max_tokens=incoming['max_tokens'],timeout=incoming['timeout'],stream=False,
            response_format={'type':'json_object'},extra_body={'thinking':{'type':'disabled'}})
        choice=response.choices[0] if response.choices else None;usage=response.usage
        return {'content':choice.message.content if choice else None,'finish_reason':choice.finish_reason if choice else None,
            'model':response.model,'usage':{'prompt_tokens':usage.prompt_tokens,'completion_tokens':usage.completion_tokens} if usage else None}
    finally:client.close()

if __name__=='__main__':
    try:print(json.dumps(sdk_response(json.load(sys.stdin)),ensure_ascii=False))
    except Exception:sys.exit(74)  # Do not print SDK exception bodies or headers.

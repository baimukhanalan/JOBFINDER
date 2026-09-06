"""Read-only allowlisted evidence for provider-controlled module transitions.

Do not retain question payloads, answer objects, candidate identifiers or auth.
This observer explains a terminal transition; it never changes the response.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, unquote

PATHS = frozenset(('/api/v1/test/switch-module', '/api/v1/test/end',
                   '/api/v1/qb/get-next-question'))


def transition_evidence(path: str, request_body: bytes, response_body: bytes) -> dict:
    if path not in PATHS:
        raise ValueError('unsupported transition endpoint')
    result = {'path': path}
    request = parse_qs(request_body.decode('utf-8', errors='replace'))
    result['request'] = {
        key: values[0] for key in ('prevModuleId', 'moduleId', 'moduleStatus', 'exitType', 'nextQuestionNumber')
        if len(values := request.get(key, [])) == 1
        and values[0].isascii() and values[0].isdecimal() and len(values[0]) <= 12
    }
    encoded = request.get('answerObject', [])
    result['request']['answer_object_present'] = bool(encoded and encoded[0] not in ('', 'null', 'undefined'))
    if len(encoded) == 1 and result['request']['answer_object_present'] and len(encoded[0]) <= 1_000_000:
        try:
            answer = json.loads(unquote(base64.b64decode(encoded[0], validate=True).decode('utf-8')))
            response = answer.get('answerResponse') if isinstance(answer, dict) else None
            result['request']['answer_response_present'] = isinstance(answer, dict) and 'answerResponse' in answer
            result['request']['answer_response_kind'] = type(response).__name__
            result['request']['answer_response_sha256'] = hashlib.sha256(
                json.dumps(response, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            if type(response) is int and 0 <= response <= 100000:
                result['request']['answer_response_scalar'] = response
            elif isinstance(response, str) and re.fullmatch(r'[0-9]{1,6}|[A-Ha-h]', response):
                result['request']['answer_response_scalar'] = response
            message = response.get('message') if isinstance(response, dict) else None
            score = message.get('score') if isinstance(message, dict) else None
            if type(score) in (int, float) and score in (0, 1):
                result['request']['simulation_score'] = score
            if isinstance(answer, dict) and isinstance(answer.get('timeLapsed'), bool):
                result['request']['time_lapsed'] = answer['timeLapsed']
        except (ValueError, UnicodeError):
            result['request']['answer_parse_error'] = True
    try:
        value = json.loads(response_body)
    except (ValueError, UnicodeError):
        result['response_parse_error'] = True
        return result
    data = value.get('data') if isinstance(value, dict) else None
    if not isinstance(data, dict):
        result['response_data_kind'] = type(data).__name__
        return result
    result['response'] = {
        key: data[key] for key in ('moduleSwitched', 'isResponseReUsed', 'isModuleSubmitted')
        if isinstance(data.get(key), bool)
    }
    for key in ('moduleId', 'questionNumber', 'moduleStatus'):
        number = data.get(key)
        if type(number) is int and 0 <= number < 10**12:
            result['response'][key] = number
    cutoff = data.get('cutOffData')
    result['response']['cutoff_present'] = isinstance(cutoff, dict)
    if isinstance(cutoff, dict) and isinstance(cutoff.get('cutOffCleared'), bool):
        result['response']['cutoff_cleared'] = cutoff['cutOffCleared']
    return result

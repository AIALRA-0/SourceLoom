import json

import pytest

from sourceloom.providers import (
    ROUTER_WEB_MAX_PROMPT_CHARACTERS,
    router_task_prompt,
    validate_router_web_prompt,
)


def task(objective='Explain this to a beginner'):
    return {
        'objective':objective,
        'taskKind':'review',
        'expectedOutput':'A JSON object conforming exactly to validation.responseSchema',
        'validation':{'responseSchema':{'type':'object'},'checks':[],'acceptanceTests':[]},
        'permissions':{
            'preset':'restricted',
            'filesystem':'read',
            'network':'none',
            'allowedHosts':[],
            'requireApprovalForWrites':False,
            'requireApprovalForExternalActions':False,
        },
        'executionChannel':'chatgpt_web',
    }


def test_router_prompt_matches_public_contract_shape():
    rendered=router_task_prompt(task())
    prefix=(
        'Complete the following task contract and return only the final deliverable.\n\n'
        'Do not delegate this task to another agent.\n\n'
    )
    assert rendered.startswith(prefix)
    contract=json.loads(rendered.removeprefix(prefix))
    assert contract=={
        'objective':'Explain this to a beginner',
        'required_context':[],
        'constraints':[],
        'expected_output':'A JSON object conforming exactly to validation.responseSchema',
        'validation_checks':[],
        'acceptance_tests':[],
        'permissions':task()['permissions'],
    }


def test_router_web_prompt_accepts_safe_short_request():
    assert validate_router_web_prompt(task())<ROUTER_WEB_MAX_PROMPT_CHARACTERS


def test_router_web_prompt_rejects_large_request_before_dispatch():
    oversized=task('x'*ROUTER_WEB_MAX_PROMPT_CHARACTERS)
    with pytest.raises(ValueError,match='未发送请求，请改用长输入 API 通道'):
        validate_router_web_prompt(oversized)

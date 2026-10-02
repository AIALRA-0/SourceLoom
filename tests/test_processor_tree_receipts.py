"""A lost response can be read back without repeating a metadata mutation."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sourceloom.processor_tree_api import register_tree
from sourceloom.store import Store


def test_management_receipt_readback_does_not_repeat_action(tmp_path):
    app = FastAPI()
    store = Store(tmp_path)
    register_tree(app, store)
    with TestClient(app) as client:
        assert client.get('/api/processor/tree/receipts/missing').json() == {
            'request_id': 'missing', 'status': 'not_found'}
        result = client.post('/api/processor/tree/actions', json={
            'action': 'create_folder', 'request_id': 'create-one', 'title': '文献'}).json()
        receipt = client.get('/api/processor/tree/receipts/create-one').json()
        assert receipt == {'request_id': 'create-one', 'status': 'completed', 'result': result}
        assert client.get('/api/processor/tree/receipts/create-one').json() == receipt
        assert client.get('/api/processor/tree').json()['total'] == 1

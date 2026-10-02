import copy

from fastapi.testclient import TestClient

from sourceloom import processor
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.store import Store, identity


def test_old_management_routes_cannot_bypass_current_tree_permissions(tmp_path):
    config = load_config()
    config.update(data_dir=str(tmp_path), auth_mode='local')
    with TestClient(create_app(config)) as client:
        store = Store(tmp_path)
        p = processor.create(store, 'Frozen source title')
        before = copy.deepcopy(p)
        headers = {'origin':'http://testserver', 'x-sourceloom':'1'}
        created = client.post('/api/processor/tree/actions', headers=headers,
            json={'action':'create_folder', 'title':'Current folder', 'request_id':identity()}).json()['node']
        fid = created['id']
        assert client.patch('/api/library/documents/'+p['id'], headers=headers,
            json={'title':'bypass', 'folder':fid, 'library_revision':0}).status_code == 409
        assert client.patch('/api/library/folders/'+fid, headers=headers,
            json={'name':'bypass', 'revision':0}).status_code == 409
        assert client.delete('/api/library/folders/'+fid, headers=headers).status_code == 409
        assert client.post('/api/library/folders', headers=headers,
            json={'name':'legacy child', 'parent':fid}).status_code == 409
        legacy = store.create('Legacy archive')
        assert client.patch('/api/library/documents/'+legacy['id'], headers=headers,
            json={'folder':fid, 'library_revision':0}).status_code == 409
        assert store.get(p['id']) == before
        assert client.get('/api/processor/tree').json()['total'] == 2


def test_legacy_parent_cannot_move_or_delete_current_descendant(tmp_path):
    config = load_config()
    config.update(data_dir=str(tmp_path), auth_mode='local')
    with TestClient(create_app(config)) as client:
        headers = {'origin':'http://testserver', 'x-sourceloom':'1'}
        legacy = client.post('/api/library/folders', headers=headers, json={'name':'Existing archive'}).json()
        store = Store(tmp_path)
        p = processor.create(store, 'Existing preserved material')
        store.change(p['id'], lambda row:row.update(folder=legacy['id']))
        baseline = copy.deepcopy(store.get(p['id']))
        assert client.patch('/api/library/folders/'+legacy['id'], headers=headers,
            json={'name':'bypass', 'revision':0}).status_code == 409
        assert client.delete('/api/library/folders/'+legacy['id'], headers=headers).status_code == 409
        assert store.get(p['id']) == baseline
        # Unrelated archived folders retain their existing management behavior.
        other = client.post('/api/library/folders', headers=headers, json={'name':'Unrelated archive'}).json()
        assert client.patch('/api/library/folders/'+other['id'], headers=headers,
            json={'name':'Renamed archive', 'revision':0}).status_code == 200

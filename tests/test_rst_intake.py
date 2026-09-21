from sourceloom.ingest import intake
from sourceloom.store import Store


def test_sphinx_function_directives_survive_rst_intake(tmp_path):
    source = b""".. module:: grp
   :synopsis: The group database.

Intro.

.. availability:: Unix, not WASI.

.. function:: getgrgid(id)

   Return the group entry.

   .. versionchanged:: 3.10
      TypeError is raised.

.. function:: getgrall()

   Return all entries.
"""
    result = intake(Store(tmp_path), [("sample.rst", source)])
    text = "\n".join(item["text"] for item in result["objects"])
    assert "module grp" in text and "The group database" in text
    assert "availability Unix, not WASI" in text
    assert "function getgrgid(id)" in text and "TypeError is raised" in text
    assert "function getgrall()" in text and "Return all entries" in text

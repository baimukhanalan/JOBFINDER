"""The live co-pilot filechooser interceptor must NOT attach the résumé PDF to a
photo/avatar/headshot upload (it used to feed the résumé into ANY file dialog)."""
from backend.copilot import _is_photo_input


def test_photo_inputs_rejected():
    assert _is_photo_input({"acc": "image/*", "blob": "upload"})
    assert _is_photo_input({"acc": "", "blob": "profile photo / headshot"})
    assert _is_photo_input({"acc": ".pdf,.doc", "blob": "upload your avatar"})
    assert _is_photo_input({"acc": "image/png,image/jpeg", "blob": "picture"})


def test_resume_inputs_accepted():
    assert not _is_photo_input({"acc": "application/pdf", "blob": "resume / cv upload"})
    assert not _is_photo_input({"acc": "", "blob": "attach your resume here"})
    assert not _is_photo_input({"acc": "", "blob": "cover letter"})
    assert not _is_photo_input({"acc": ".pdf", "blob": "autofill from resume"})

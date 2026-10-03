"""Physical-device control (PTZ, lights, siren, IR) behind virtual ONVIF cameras.

See controller.py for the object model and onvif_control.py for how it is
exposed to the NVR over ONVIF.
"""

from .controller import CameraControl, ControlError, registry  # noqa: F401

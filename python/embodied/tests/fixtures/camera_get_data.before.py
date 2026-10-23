"""Pinned RoboDojo camera method for the frame-availability regression."""
from __future__ import annotations

class CameraView:
    def get_data(
        self,
        annotator_type: str,
        *,
        tiled: bool = False,
        out: wp.array | None = None,
    ) -> Tuple[wp.array, dict[str, Any]]:
        """Fetch the specified annotator/sensor data for all cameras as a batch of images or as a single tiled image.

        Args:
            annotator_type: Annotator/sensor type from which fetch the data.
            tiled: Whether to get annotator/sensor data as a single tiled image.
            out: Pre-allocated array to fill with the fetched data.

        Returns:
            2-items tuple. The first item is an array containing the fetched data (if ``out`` is defined,
            its instance will be returned). The second item is a dictionary containing additional information according
            to the requested annotator/sensor type.

        Raises:
            ValueError: If the specified annotator type is not supported.
            ValueError: If the specified annotator type is not configured when instantiating the object.
        """
        # get and check annotator specification
        spec = ANNOTATOR_SPEC.get(annotator_type)
        if spec is None:
            raise ValueError(
                f"Unsupported annotator type: {annotator_type}. Supported types are {list(ANNOTATOR_SPEC.keys())}"
            )
        if spec["name"] not in self._annotators:
            raise ValueError(
                f"The specified annotator type ({annotator_type}) was not configured. Enable it when instantiating the object"
            )
        # request data on the same device as output if specified
        # If out is provided, use its device; otherwise use CUDA for better performance
        if out is not None:
            output_device = str(out.device) if hasattr(out, "device") else "cuda"
        else:
            output_device = "cuda"
        # get the linear sensor data from the tiled annotator and (if needed) slice it to get only the RGB data
        data = self._annotators[spec["name"]].get_data(device=output_device)
        # check whether returned data is a dict (used for segmentation)
        if isinstance(data, dict):
            tiled_data: wp.array = data["data"]
            info = data["info"]
        else:
            tiled_data: wp.array = data
            info = {}
        # tiled image
        if tiled:
            shape = (*self.tiled_resolution, spec["channels"])
            if out is None:
                out = wp.clone(tiled_data[:, :, :3]) if annotator_type == "rgb" else tiled_data.reshape(shape)
            else:
                wp.copy(
                    out,
                    tiled_data[:, :, :3] if annotator_type == "rgb" else tiled_data.reshape(shape),
                )
        # batched images
        else:
            # define internal variables
            channels = spec["channels"]
            # rgb uses rgba (4 channels), not 3
            output_channels = channels  # Use channels directly (rgb has 4 channels from rgba)
            width, height = self.camera_resolution
            num_cameras = len(self.prims)

            # check if the data should be copied to the pre-allocated memory
            if out is None:
                # Allocate new buffer only if not provided
                shape = (num_cameras, height, width, output_channels)
                out = wp.zeros(shape, dtype=spec["dtype"], device=tiled_data.device)
            else:
                # Verify output buffer shape matches expected shape
                expected_shape = (num_cameras, height, width, output_channels)
                if hasattr(out, "shape") and out.shape != expected_shape:
                    # Buffer shape mismatch, reallocate
                    out = wp.zeros(expected_shape, dtype=spec["dtype"], device=tiled_data.device)
                # Ensure output is on the same device as tiled_data
                if hasattr(out, "device") and hasattr(tiled_data, "device"):
                    if out.device != tiled_data.device:
                        # Reallocate on correct device if needed
                        out = wp.zeros(
                            (num_cameras, height, width, output_channels),
                            dtype=spec["dtype"],
                            device=tiled_data.device,
                        )

            # use a warp kernel to convert the linear sensor data to a batch of images
            num_tiles_x = self.tiled_resolution[0] // width
            wp.launch(
                kernel=reshape_tiled_image,
                dim=(num_cameras, height, width),
                inputs=[
                    tiled_data.flatten(),
                    out,
                    height,
                    width,
                    channels,
                    output_channels,
                    num_tiles_x,
                    0,  # offset is always 0 since we sliced the data
                ],
                device=tiled_data.device,
            )
        return out, info

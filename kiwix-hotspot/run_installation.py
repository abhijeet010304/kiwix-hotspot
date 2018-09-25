# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4 nu

import os
import sys
import re
import time
import shutil
import traceback
from datetime import datetime

import data
from backend import qemu
from util import (
    human_readable_size,
    get_cache,
    ensure_zip_exfat_compatible,
    EXFAT_FORBIDDEN_CHARS,
)

from backend import ansiblecube
from backend.content import (
    get_collection,
    get_content,
    get_all_contents_for,
    isremote,
    get_content_cache,
    get_alien_content,
)
from backend.download import download_content, unzip_file
from backend.mount import (
    mount_data_partition,
    unmount_data_partition,
    test_mount_procedure,
    format_data_partition,
    guess_next_loop_device,
)
from backend.util import ensure_card_written, ImageWriterThread
from backend.mount import can_write_on, allow_write_on, restore_mode
from backend.util import (
    subprocess_pretty_check_call,
    prevent_sleep,
    restore_sleep_policy,
)
from backend.sysreq import host_matches_requirements, requirements_url


def run_installation(
    name,
    timezone,
    language,
    wifi_pwd,
    admin_account,
    kalite,
    aflatoun,
    wikifundi,
    edupi,
    edupi_resources,
    zim_install,
    size,
    logger,
    cancel_event,
    sd_card,
    favicon,
    logo,
    css,
    done_callback=None,
    build_dir=".",
    qemu_ram="2G",
):

    logger.start(bool(sd_card))

    logger.stage("init")
    cache_folder = get_cache(build_dir)

    try:
        logger.std("Preventing system from sleeping")
        sleep_ref = prevent_sleep(logger)

        logger.step("Check System Requirements")
        logger.std("Please read {} for details".format(requirements_url))
        sysreq_ok, missing_deps = host_matches_requirements(build_dir)
        if not sysreq_ok:
            raise SystemError(
                "Your system does not matches system requirements:\n{}".format(
                    "\n".join([" - {}".format(dep) for dep in missing_deps])
                )
            )

        logger.step("Ensure user files are present")
        for user_fpath in (edupi_resources, favicon, logo, css):
            if (
                user_fpath is not None
                and not isremote(user_fpath)
                and not os.path.exists(user_fpath)
            ):
                raise ValueError(
                    "Specified file is not available ({})".format(user_fpath)
                )

        logger.step("Prepare Image file")

        # set image names
        today = datetime.today().strftime("%Y_%m_%d-%H_%M_%S")

        image_final_path = os.path.join(build_dir, "hotspot-{}.img".format(today))
        image_building_path = os.path.join(
            build_dir, "hotspot-{}.BUILDING.img".format(today)
        )
        image_error_path = os.path.join(build_dir, "hotspot-{}.ERROR.img".format(today))

        # loop device mode on linux (for mkfs in userspace)
        if sys.platform == "linux":
            loop_dev = guess_next_loop_device(logger)
            if loop_dev and not can_write_on(loop_dev):
                logger.step("Change loop device mode ({})".format(sd_card))
                previous_loop_mode = allow_write_on(loop_dev, logger)
            else:
                previous_loop_mode = None

        # Prepare SD Card
        if sd_card:
            logger.step("Change SD-card device mode ({})".format(sd_card))
            if sys.platform == "linux":
                allow_write_on(sd_card, logger)
            elif sys.platform == "darwin":
                subprocess_pretty_check_call(
                    ["diskutil", "unmountDisk", sd_card], logger
                )
                allow_write_on(sd_card, logger)
            elif sys.platform == "win32":
                logger.step("Format SD card {}".format(sd_card))
                matches = re.findall(r"\\\\.\\PHYSICALDRIVE(\d*)", sd_card)
                if len(matches) != 1:
                    raise ValueError("Error while getting physical drive number")
                device_number = matches[0]

                r, w = os.pipe()
                os.write(w, str.encode("select disk {}\n".format(device_number)))
                os.write(w, b"clean\n")
                os.close(w)
                logger.std("diskpart select disk {} and clean".format(device_number))
                subprocess_pretty_check_call(["diskpart"], logger, stdin=r)
                logger.std("sleeping for 15s to acknowledge diskpart changes")
                time.sleep(15)

        # Download Base image
        logger.stage("master")
        logger.step("Retrieving base image file")
        base_image = get_content("hotspot_master_image")
        rf = download_content(base_image, logger, build_dir)
        if not rf.successful:
            logger.err("Failed to download base image.\n{e}".format(e=rf.exception))
            sys.exit(1)
        elif rf.found:
            logger.std("Reusing already downloaded base image ZIP file")
        logger.progress(.5)

        # extract base image and rename
        logger.step("Extracting base image from ZIP file")
        unzip_file(
            archive_fpath=rf.fpath,
            src_fname=base_image["name"].replace(".zip", ""),
            build_folder=build_dir,
            dest_fpath=image_building_path,
        )
        logger.std("Extraction complete: {p}".format(p=image_building_path))
        logger.progress(.9)

        if not os.path.exists(image_building_path):
            raise IOError("image path does not exists: {}".format(image_building_path))

        logger.step("Testing mount procedure")
        if not test_mount_procedure(image_building_path, logger, True):
            raise ValueError("thorough mount procedure failed")

        # wait for QEMU to release file (windows mostly)
        logger.succ("Image creation successful.")
        time.sleep(20)

    except Exception as e:
        logger.failed(str(e))

        # display traceback on logger
        logger.std(
            "\n--- Exception Trace ---\n{exp}\n---".format(exp=traceback.format_exc())
        )

        # Set final image filename
        if os.path.isfile(image_building_path):
            os.rename(image_building_path, image_error_path)

        error = e
    else:
        try:
            # Set final image filename
            tries = 0
            while True:
                try:
                    os.rename(image_building_path, image_final_path)
                except Exception as exp:
                    logger.err(exp)
                    tries += 1
                    if tries > 3:
                        raise exp
                    time.sleep(5 * tries)
                    continue
                else:
                    logger.std("Renamed image file to {}".format(image_final_path))
                    break

            # Write image to SD Card
            if sd_card:
                logger.stage("write")
                logger.step("Writting image to SD-card ({})".format(sd_card))
                try:
                    imwriter = ImageWriterThread(
                        args=(image_final_path, sd_card, logger)
                    )
                    cancel_event.register_thread(thread=imwriter)
                    imwriter.start()
                    imwriter.join(timeout=2)  # make sure it started
                    while imwriter.is_alive():
                        pass
                    imwriter.join(timeout=2)
                    cancel_event.unregister_thread()
                    if imwriter.exp is not None:
                        raise imwriter.exp

                    logger.std("Done writing ; preparing for verification.")
                    time.sleep(5)
                    ensure_card_written(image_final_path, sd_card, logger)

                except ValueError:
                    logger.succ("Image created successfuly.")
                    logger.err(
                        "SD-card content is different than that of image.\n"
                        "Please check the content of your card and "
                        "verify that the card is not damaged ("
                        "often turns read-only silently).\n"
                        "Alternatively, use Etcher (see File > Flash) to "
                        "flash image onto SD-card and validate transfer."
                    )
                    raise Exception("SD-card content verification failed")
                except Exception:
                    logger.succ("Image created successfuly.")
                    logger.err(
                        "Writing your Image to your SD-card failed.\n"
                        "Please use a third party tool to flash your image "
                        "onto your SD-card. See File menu for links to Etcher."
                    )
                    raise Exception("Failed to write Image to SD-card")

        except Exception as e:
            logger.failed(str(e))

            # display traceback on logger
            logger.std(
                "\n--- Exception Trace ---\n{exp}\n---".format(
                    exp=traceback.format_exc()
                )
            )
            error = e
        else:
            logger.complete()
            error = None
    finally:
        logger.std("Restoring system sleep policy")
        restore_sleep_policy(sleep_ref, logger)

        if sys.platform == "linux" and loop_dev and previous_loop_mode:
            logger.step("Restoring loop device ({}) mode".format(loop_dev))
            restore_mode(loop_dev, previous_loop_mode, logger)

        if sd_card:
            logger.step("Restoring SD-card device ({}) mode".format(sd_card))
            if sys.platform == "linux":
                restore_mode(sd_card, "0660", logger)
            elif sys.platform == "darwin":
                restore_mode(sd_card, "0660", logger)

        # display durations summary
        logger.summary()

    if done_callback:
        done_callback(error)

    return error

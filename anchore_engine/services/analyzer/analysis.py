import json
import os
import time
import typing

import anchore_engine.clients
import anchore_engine.subsys
from anchore_engine.analyzers.manager import AnalysisResult
from anchore_engine.clients import localanchore_standalone
from anchore_engine.clients.services import internal_client_for
from anchore_engine.clients.services.catalog import CatalogClient
from anchore_engine.clients.services.policy_engine import PolicyEngineClient
from anchore_engine.common import helpers
from anchore_engine.common.models.schemas import AnalysisQueueMessage, ValidationError
from anchore_engine.configuration.localconfig import get_config
from anchore_engine.services.analyzer.errors import (
    CatalogClientError,
    PolicyEngineClientError,
)
from anchore_engine.services.analyzer.tasks import WorkerTask
from anchore_engine.services.analyzer.utils import (
    emit_events,
    get_tempdir,
    update_analysis_complete,
    update_analysis_failed,
    update_analysis_started,
)
from anchore_engine.subsys import events, logger
from anchore_engine.subsys.events.util import (
    analysis_complete_notification_factory,
    fulltag_from_detail,
)
from anchore_engine.utils import AnchoreException

ANALYSIS_TIME_SECONDS_BUCKETS = [
    1.0,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1800.0,
    3600.0,
]


def notify_analysis_complete(
    image_record: dict, last_analysis_status
) -> typing.List[events.UserAnalyzeImageCompleted]:
    """

    :param image_record:
    :return: list of UserAnalyzeImageCompleted events, one for each tag in the image record
    """

    events = []
    image_digest = image_record["imageDigest"]
    account = image_record["userId"]

    annotations = {}
    try:
        if image_record.get("annotations", "{}"):
            annotations = json.loads(image_record.get("annotations", "{}"))
    except Exception as err:
        logger.warn("could not marshal annotations from json - exception: " + str(err))

    # Re-pull the image record before sending notifications
    # For the case where new tags for the image were added
    catalog_client = internal_client_for(CatalogClient, account)
    try:
        image_record = get_image_record(catalog_client, image_digest)
    except Exception as err:
        logger.warn(
            "Cannot re-get image from catalog for image digest %s, generating notifications for already-known tags",
            image_digest,
        )

    for image_detail in image_record["image_detail"]:
        fulltag = fulltag_from_detail(image_detail)
        event = analysis_complete_notification_factory(
            account,
            image_digest,
            last_analysis_status,
            image_record["analysis_status"],
            fulltag,
            annotations,
        )
        events.append(event)

    return events


def is_analysis_message(payload_json: dict) -> bool:
    """
    Is the given payload an analysis message payload or some other kind
    :param payload_json:
    :return:
    """
    try:
        return AnalysisQueueMessage.from_json(payload_json) is not None
    except ValidationError:
        return False


def perform_analyze(
    account,
    manifest,
    image_record,
    registry_creds,
    layer_cache_enable=False,
    parent_manifest=None,
    owned_package_filtering_enabled: bool = True,
) -> AnalysisResult:
    ret_analyze = {}

    loaded_config = get_config()
    tmpdir = get_tempdir(loaded_config)

    use_cache_dir = None
    if layer_cache_enable:
        use_cache_dir = os.path.join(tmpdir, "anchore_layercache")

    # choose the first TODO possible more complex selection here
    try:
        image_detail = image_record["image_detail"][0]
        registry_manifest = manifest
        registry_parent_manifest = parent_manifest
        pullstring = (
            image_detail["registry"]
            + "/"
            + image_detail["repo"]
            + "@"
            + image_detail["imageDigest"]
        )
        fulltag = (
            image_detail["registry"]
            + "/"
            + image_detail["repo"]
            + ":"
            + image_detail["tag"]
        )
        logger.debug(
            "using pullstring ("
            + str(pullstring)
            + ") and fulltag ("
            + str(fulltag)
            + ") to pull image data"
        )
    except Exception as err:
        image_detail = pullstring = fulltag = None
        raise Exception(
            "failed to extract requisite information from image_record - exception: "
            + str(err)
        )

    timer = int(time.time())
    logger.spew("timing: analyze start: " + str(int(time.time()) - timer))
    logger.info("performing analysis on image: " + str([account, pullstring, fulltag]))

    logger.debug("obtaining anchorelock..." + str(pullstring))
    with anchore_engine.clients.localanchore_standalone.get_anchorelock(
        lockId=pullstring, driver="nodocker"
    ):
        logger.debug("obtaining anchorelock successful: " + str(pullstring))
        logger.info("analyzing image: %s", pullstring)
        result = localanchore_standalone.analyze_image(
            account,
            registry_manifest,
            image_record,
            tmpdir,
            loaded_config,
            registry_creds=registry_creds,
            use_cache_dir=use_cache_dir,
            parent_manifest=registry_parent_manifest,
            owned_package_filtering_enabled=owned_package_filtering_enabled,
        )

    logger.info("performing analysis on image complete: %s", pullstring)

    return result


def build_catalog_url(account: str, image_digest: str) -> str:
    """
    Returns the URL as a string that the policy engine will use to fetch the loaded analysis result from the catalog
    :param account:
    :param image_digest:
    :return:
    """
    return "catalog://{}/analysis_data/{}".format(account, image_digest)


def import_to_policy_engine(account: str, image_id: str, image_digest: str):
    """
    Import the given image into the policy engine

    :param account:
    :param image_id:
    :param image_digest:
    :return:
    """
    if image_id is None:
        raise ValueError("image_id must not be None")

    pe_client = internal_client_for(PolicyEngineClient, account)

    try:
        logger.debug(
            "clearing any existing image record in policy engine: {} / {} / {}".format(
                account, image_id, image_digest
            )
        )
        rc = pe_client.delete_image(user_id=account, image_id=image_id)
    except Exception as err:
        logger.warn("exception on pre-delete - exception: " + str(err))

    client_success = False
    last_exception = None

    # TODO: rework this wait logic using 'retrying.retry()' decorator on this whole function to allow new client on each call
    for retry_wait in [1, 3, 5, 0]:
        try:
            logger.info(
                "loading image into policy engine: account={} image_id={} image_digest={}".format(
                    account, image_id, image_digest
                )
            )
            image_analysis_fetch_url = build_catalog_url(account, image_digest)
            logger.debug(
                "policy engine request catalog content url: " + image_analysis_fetch_url
            )
            resp = pe_client.ingress_image(account, image_id, image_analysis_fetch_url)
            logger.debug("policy engine image add response: " + str(resp))
            client_success = True
            break
            # TODO: add a vuln eval and policy eval (with active policy), here to prime any caches since this isn't a highly latency sensitive code section
        except Exception as e:
            logger.warn("attempt failed, will retry - exception: {}".format(e))
            last_exception = e
            time.sleep(retry_wait)

    if not client_success:
        raise last_exception

    return True


def process_analyzer_job(
    request: AnalysisQueueMessage,
    layer_cache_enable,
    owned_package_filtering_enabled: bool = True,
):
    """
    Core logic of the analysis process

    :param request:
    :param layer_cache_enable:
    :param owned_package_filtering_enabled: feature flag for whether or not the analyzer will perform owned package filtering
    :return:
    """

    timer = int(time.time())
    analysis_events = []

    loaded_config = get_config()
    all_content_types = loaded_config.get(
        "image_content_types", []
    ) + loaded_config.get("image_metadata_types", [])

    try:
        account = request.account
        image_digest = request.image_digest
        manifest = request.manifest
        parent_manifest = request.parent_manifest

        # check to make sure image is still in DB
        catalog_client = internal_client_for(CatalogClient, account)
        try:
            image_record = get_image_record(catalog_client, image_digest)
        except Exception as err:
            logger.warn(
                "dequeued image cannot be fetched from catalog - skipping analysis ("
                + str(image_digest)
                + ") - exception: "
                + str(err)
            )
            return True

        logger.info(
            "image dequeued for analysis: " + str(account) + " : " + str(image_digest)
        )
        if image_record[
            "analysis_status"
        ] != anchore_engine.subsys.taskstate.base_state("analyze"):
            logger.debug("dequeued image is not in base state - skipping analysis")
            return True

        try:
            logger.spew("TIMING MARK0: " + str(int(time.time()) - timer))

            last_analysis_status = image_record["analysis_status"]
            image_record = update_analysis_started(
                catalog_client, image_digest, image_record
            )

            # actually do analysis
            registry_creds = catalog_client.get_registry()
            try:
                analysis_result = perform_analyze(
                    account,
                    manifest,
                    image_record,
                    registry_creds,
                    layer_cache_enable=layer_cache_enable,
                    parent_manifest=parent_manifest,
                    owned_package_filtering_enabled=owned_package_filtering_enabled,
                )
            except AnchoreException as e:
                event = events.ImageAnalysisFailed(
                    user_id=account, image_digest=image_digest, error=e.to_dict()
                )
                analysis_events.append(event)
                raise

            image_data = analysis_result.image_export
            syft_analysis = analysis_result.syft_output

            # Save the results to the upstream components and data stores
            store_analysis_results(
                account,
                image_digest,
                image_record,
                image_data,
                manifest,
                analysis_events,
                all_content_types,
                syft_report=syft_analysis,
            )

            logger.debug("updating image catalog record analysis_status")
            last_analysis_status = image_record["analysis_status"]
            image_record = update_analysis_complete(
                catalog_client, image_digest, image_record
            )

            try:
                analysis_events.extend(
                    notify_analysis_complete(image_record, last_analysis_status)
                )
            except Exception as err:
                logger.warn(
                    "failed to enqueue notification on image analysis state update - exception: "
                    + str(err)
                )

            logger.info(
                "analysis complete: " + str(account) + " : " + str(image_digest)
            )
            logger.spew("TIMING MARK1: " + str(int(time.time()) - timer))

            try:
                anchore_engine.subsys.metrics.counter_inc(
                    name="anchore_analysis_success"
                )
                run_time = float(time.time() - timer)

                anchore_engine.subsys.metrics.histogram_observe(
                    "anchore_analysis_time_seconds",
                    run_time,
                    buckets=ANALYSIS_TIME_SECONDS_BUCKETS,
                    status="success",
                )

            except Exception as err:
                logger.warn(str(err))
                pass

        except Exception as err:
            run_time = float(time.time() - timer)
            logger.exception("problem analyzing image - exception: " + str(err))
            analysis_failed_metrics(run_time)

            # Transition the image record to failure status
            image_record = update_analysis_failed(
                catalog_client, image_digest, image_record
            )

            if account and image_digest:
                for image_detail in image_record["image_detail"]:
                    fulltag = (
                        image_detail["registry"]
                        + "/"
                        + image_detail["repo"]
                        + ":"
                        + image_detail["tag"]
                    )
                    event = events.UserAnalyzeImageFailed(
                        user_id=account, full_tag=fulltag, error=str(err)
                    )
                    analysis_events.append(event)
        finally:
            if analysis_events:
                emit_events(catalog_client, analysis_events)

    except Exception as err:
        logger.warn("job processing bailed - exception: " + str(err))
        raise err

    return True


def get_image_record(catalog_client, image_digest):
    image_record = catalog_client.get_image(image_digest)
    if not image_record:
        raise Exception("empty image record from catalog")
    return image_record


def analysis_sucess_metrics(analysis_time: float, allow_exception=False):
    try:
        anchore_engine.subsys.metrics.counter_inc(name="anchore_analysis_success")
        anchore_engine.subsys.metrics.histogram_observe(
            "anchore_analysis_time_seconds",
            analysis_time,
            buckets=ANALYSIS_TIME_SECONDS_BUCKETS,
            status="success",
        )
    except:
        if allow_exception:
            raise
        else:
            logger.exception(
                "Unexpected exception during metrics update for a successful analysis. Swallowing error and continuing"
            )


def analysis_failed_metrics(analysis_time: float, allow_exception=False):
    try:
        anchore_engine.subsys.metrics.counter_inc(name="anchore_analysis_error")
        anchore_engine.subsys.metrics.histogram_observe(
            "anchore_analysis_time_seconds",
            analysis_time,
            buckets=ANALYSIS_TIME_SECONDS_BUCKETS,
            status="fail",
        )
    except:
        if allow_exception:
            raise
        else:
            logger.exception(
                "Unexpected exception during metrics update for a failed analysis. Swallowing error and continuing"
            )


def store_analysis_results(
    account: str,
    image_digest: str,
    image_record: dict,
    analysis_result: list,
    image_manifest: dict,
    analysis_events: list,
    image_content_types: list,
    syft_report: dict = None,
):
    """

    :param account:
    :param image_digest:
    :param image_record:
    :param analysis_result:
    :param image_manifest:
    :param analysis_events: list of events that any new events may be added to
    :param image_content_types:
    :param syft_report:
    :return:
    """

    try:
        catalog_client = internal_client_for(CatalogClient, account)
    except:
        logger.debug_exception("Cannot instantiate a catalog client to upload results")
        raise

    imageId = None
    try:
        imageId = analysis_result[0]["image"]["imageId"]
    except Exception as err:
        logger.warn(
            "could not get imageId after analysis or from image record - exception: "
            + str(err)
        )

    logger.info(
        "adding image analysis data to catalog: account={} imageId={} imageDigest={}".format(
            account, imageId, image_digest
        )
    )

    if syft_report:
        try:
            logger.info("Saving raw syft output data to catalog object store")
            rc = catalog_client.put_document("syft_sbom", image_digest, syft_report)
            if not rc:
                # Ugh this ia big ugly, but need to be sure. Should review CatalogClient and ensure this cannot happen, but for now just handle it.
                raise CatalogClientError(
                    msg="Catalog client returned failure",
                    cause="Invalid response from catalog API - {}".format(str(rc)),
                )
        except Exception as e:
            err = CatalogClientError(
                msg="Failed to upload analysis data to catalog", cause=e
            )
            event = events.SaveAnalysisFailed(
                user_id=account, image_digest=image_digest, error=err.to_dict()
            )
            analysis_events.append(event)
            raise err

    try:
        logger.info("Saving raw analysis data to catalog object store")
        rc = catalog_client.put_document("analysis_data", image_digest, analysis_result)
        if not rc:
            # Ugh this ia big ugly, but need to be sure. Should review CatalogClient and ensure this cannot happen, but for now just handle it.
            raise CatalogClientError(
                msg="Catalog client returned failure",
                cause="Invalid response from catalog API - {}".format(str(rc)),
            )
    except Exception as e:
        err = CatalogClientError(
            msg="Failed to upload analysis data to catalog", cause=e
        )
        event = events.SaveAnalysisFailed(
            user_id=account, image_digest=image_digest, error=err.to_dict()
        )
        analysis_events.append(event)
        raise err

    try:
        logger.info("Extracting and normalizing image content data locally")
        image_content_data = {}

        # TODO: paginate the content data here keep payloads smaller for clients
        for content_type in image_content_types:
            try:
                image_content_data[content_type] = helpers.extract_analyzer_content(
                    analysis_result, content_type, manifest=image_manifest
                )
            except Exception as err:
                logger.warn("ERR: {}".format(err))
                image_content_data[content_type] = {}

        if image_content_data:
            logger.info("Adding image content data to archive")
            rc = catalog_client.put_document(
                "image_content_data", image_digest, image_content_data
            )

            logger.debug("adding image analysis data to image_record")
            helpers.update_image_record_with_analysis_data(
                image_record, analysis_result
            )

    except Exception as err:
        import traceback

        traceback.print_exc()
        logger.warn(
            "could not store image content metadata to archive - exception: " + str(err)
        )

    # Load the result into the policy engine
    logger.info(
        "adding image to policy engine: account={} imageId={} imageDigest={}".format(
            account, imageId, image_digest
        )
    )
    try:
        import_to_policy_engine(account, imageId, image_digest)
    except Exception as err:
        newerr = PolicyEngineClientError(
            msg="Adding image to policy-engine failed", cause=str(err)
        )
        event = events.PolicyEngineLoadAnalysisFailed(
            user_id=account, image_digest=image_digest, error=newerr.to_dict()
        )
        analysis_events.append(event)
        raise newerr


class ImageAnalysisTask(WorkerTask):
    """
    The actual analysis task
    """

    def __init__(
        self,
        message: AnalysisQueueMessage,
        layer_cache_enabled: bool = False,
        owned_package_filtering_enabled: bool = True,
    ):
        super().__init__()
        self.layer_cache_enabled = layer_cache_enabled
        self.message = message
        self.owned_package_filtering_enabled = owned_package_filtering_enabled

    def execute(self):
        return process_analyzer_job(
            self.message, self.layer_cache_enabled, self.owned_package_filtering_enabled
        )

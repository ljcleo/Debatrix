from argparse import ArgumentParser
from asyncio import CancelledError, Runner, Semaphore, Task, TaskGroup
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from shutil import copytree, rmtree
from typing import Any, Literal

from debatrix.core.common import DebateResult, Verdict
from debatrix.model import ChatModelBackend
from debatrix.platform import Platform, Session


@dataclass
class ScriptArgs:
    preset: str = ""
    framework: Literal["gpt", "non_iter", "debatrix"] = "gpt"

    start: int = 0
    stop: int = 1
    repeat: int = 1

    dimensions: list[str] | None = None
    should_summarize: bool = False

    llm: Literal["test", "chatgpt", "gpt4"] = "test"

    debug_server: bool = False
    root_dir: str = ""


async def work(session: Session, /, *, debate_id: int, args: ScriptArgs) -> None:
    config: dict[str, Any] = session.config_data

    arena_interface_config: dict[str, Any] = config["arena"]
    arena_interface_config["streaming_delay"] = 0

    chat_model_config: dict[str, Any] = config["model"]["chat_config"]

    chat_model_config["backend"] = (
        ChatModelBackend.TEST if args.llm == "test" else ChatModelBackend.OPENAI
    )

    chat_model_config["test_config"]["predict_delay"] = 0

    chat_model_config["openai_config"]["model"] = (
        "gpt-4-0125-preview" if args.llm == "gpt4" else "gpt-3.5-turbo-0125"
    )

    judge_interface_config: dict[str, Any] = config["panel"]["judge_config"]
    judge_interface_config["allow_concurrency"] = True
    judge_interface_config["allow_ai_callback"] = False
    judge_interface_config["analyze_speech"] = args.framework != "gpt"
    judge_interface_config["iterate_analysis"] = args.framework == "debatrix"

    panel_interface_config: dict[str, Any] = config["panel"]["panel_config"]
    panel_interface_config["allow_concurrency"] = True
    panel_interface_config["allow_ai_callback"] = False

    manager_config: dict[str, Any] = config["manager"]
    manager_config["should_summarize"] = args.should_summarize

    if args.dimensions is not None:
        for dimension in manager_config["dimensions"]:
            if dimension["name"] not in args.dimensions:
                dimension["weight"] = -1

    recorder_config: dict[str, Any] = config["recorder"]
    recorder_config["include_prompts"] = False
    recorder_config["verdict_only"] = True

    await session.update_config()

    motion_code: str = session.motions[debate_id][0]
    await session.select_debate(motion_code)
    assert session.cur_info is not None
    title: str = f"{motion_code} --- {session.cur_info.motion}"

    print(f"Start: {title}\n")

    try:
        result: DebateResult | None = await session.start_debate()
        print(f"End: {title}\n")

        if result is None:
            print("Debate cancelled.\n")
        else:
            if args.should_summarize:
                final_verdict: Verdict | None = result.final_verdict
                assert final_verdict is not None

                for debater_verdict in final_verdict.debaters_verdict:
                    print(f"{debater_verdict.debater_name}: {debater_verdict.score}")

                print(f"Winner: {final_verdict.winner_verdict.winner}\n")
            else:
                debaters_scores: defaultdict[str, list[int]] = defaultdict(list)
                winners: list[str] = []

                for dimension_verdict in result.dimensional_verdicts:
                    for debater_verdict in dimension_verdict.verdict.debaters_verdict:
                        debaters_scores[debater_verdict.debater_name].append(debater_verdict.score)

                    winners.append(dimension_verdict.verdict.winner_verdict.winner)

                for debater_name, scores in debaters_scores.items():
                    print(f"{debater_name}: {', '.join([str(score) for score in scores])}")

                print(f"Winner: {', '.join(winners)}\n")

        print(f"Verdict saved at {await session.save_record()}\n")
    except Exception as e:
        print(f"Failed: {e}\n")
    finally:
        await session.reset_debate()


async def main(platform: Platform, /, *, args: ScriptArgs) -> None:
    async with TaskGroup() as tg:
        task: Task[None] = tg.create_task(platform.serve())

        try:
            semaphore = Semaphore(12)

            async def wait_and_work(debate_id: int, /) -> None:
                async with semaphore:
                    await work(await platform.assign(), debate_id=debate_id, args=args)

            async with TaskGroup() as inner_tg:
                for debate_id in range(args.start, args.stop):
                    for _ in range(args.repeat):
                        inner_tg.create_task(wait_and_work(debate_id))
        finally:
            task.cancel()

            try:
                await task
            except CancelledError:
                pass


if __name__ == "__main__":
    arg_parser = ArgumentParser(description="Debatrix batch judging")

    arg_parser.add_argument("preset", help="select debate & config preset")

    arg_parser.add_argument(
        "framework", choices=("gpt", "non_iter", "debatrix"), help="switch judging framework"
    )

    arg_parser.add_argument("start", type=int, help="set preset debate start index")
    arg_parser.add_argument("stop", type=int, help="set preset debate stop index")
    arg_parser.add_argument("repeat", type=int, help="repeat judging this number of times")

    arg_parser.add_argument(
        "-d", "--dimensions", nargs="*", help="select a subset of judging dimensions"
    )

    arg_parser.add_argument(
        "-s",
        "--should-summarize",
        action="store_true",
        help="summarize judgment from different dimensions",
    )

    arg_parser.add_argument(
        "-l",
        "--llm",
        default="test",
        choices=("test", "chatgpt", "gpt4"),
        help="switch backbone LLM",
    )

    arg_parser.add_argument(
        "-v", "--debug-server", action="store_true", help="enable FastAPI debug mode"
    )

    arg_parser.add_argument(
        "-r", "--root_dir", default=".", help="choose a different root directory"
    )

    args: ScriptArgs = arg_parser.parse_args(namespace=ScriptArgs())

    preset_path: Path = Path("./preset") / args.preset
    resource_path: Path = Path(args.root_dir) / args.framework / "resource"
    resource_path.mkdir(parents=True, exist_ok=True)
    print(f"Using resource path: {resource_path.as_posix()}")

    print(f"Loading preset {args.preset} ...")
    config_path: Path = resource_path / "config"

    if config_path.exists():
        if config_path.is_symlink() or not config_path.is_dir():
            config_path.unlink()
        else:
            rmtree(config_path, ignore_errors=True)

    copytree(preset_path / "config", config_path)

    for target in ("motion", "speech"):
        target_path: Path = resource_path / target

        if target_path.exists():
            if target_path.is_symlink() or not target_path.is_dir():
                target_path.unlink()
            else:
                rmtree(target_path, ignore_errors=True)

        target_path.symlink_to((preset_path / target).absolute())

    Runner(debug=True).run(
        main(
            Platform(resource_path, fast_api_debug=args.debug_server, fast_api_log_info=False),
            args=args,
        )
    )

    print("Cleaning up ...")
    rmtree(config_path)

    for target in ("motion", "speech"):
        (resource_path / target).unlink()

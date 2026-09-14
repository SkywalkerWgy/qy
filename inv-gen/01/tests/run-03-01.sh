#!/usr/bin/env bash

set -uo pipefail

TEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="$TEST_ROOT/_runtime"
CASES_FILE="$TEST_ROOT/cases.tsv"
IMAGE="${COL2INV_IMAGE:-col2inv:nju-03-01}"
MODEL="${COL2INV_MODEL:-deepseek-flash}"
JOBS="${JOBS:-10}"
FORCE="${FORCE:-0}"
BUILD_IMAGE="${BUILD_IMAGE:-auto}"
RUN_ID="03-01-$$"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
# 优先继承启动脚本时已有的 DeepSeek 环境变量。后面再次显式 export，
# 确保并行子进程以及 `docker run --env DEEPSEEK_API_KEY` 都能读取。
DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"

usage() {
    cat <<'EOF'
用法：bash run-03-01.sh [选项]

选项：
  -j, --jobs N       并行样例数，默认 10
  -m, --model NAME   模型名称，默认 deepseek-flash
  -f, --force        清除各用例旧结果并重新运行
      --build        强制重新构建 Docker 镜像
      --no-build     不自动构建；要求镜像已经存在
  -h, --help         显示帮助

环境变量：
  DEEPSEEK_API_KEY   必填，DeepSeek API Key
  JOBS               并行样例数
  COL2INV_IMAGE      Docker 镜像名
  COL2INV_MODEL      模型名称
  FORCE=1            等价于 --force
  BUILD_IMAGE=1|0    强制构建或禁止自动构建

固定实验设置：proposal=5、单样例无时间限制、final repair 无上限、thinking 开启。
EOF
}

while (($#)); do
    case "$1" in
        -j|--jobs) JOBS="${2:?缺少并行数}"; shift 2 ;;
        -m|--model) MODEL="${2:?缺少模型名}"; shift 2 ;;
        -f|--force) FORCE=1; shift ;;
        --build) BUILD_IMAGE=1; shift ;;
        --no-build) BUILD_IMAGE=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf '未知选项：%s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

if ! [[ "$JOBS" =~ ^[1-9][0-9]*$ ]]; then
    printf '错误：并行数必须是正整数，当前值为 %s\n' "$JOBS" >&2
    exit 2
fi
if [[ ! -s "$CASES_FILE" ]]; then
    printf '错误：缺少用例清单 %s\n' "$CASES_FILE" >&2
    exit 2
fi
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
    printf '错误：当前环境中未找到 DEEPSEEK_API_KEY，请先设置该环境变量。\n' >&2
    exit 2
fi
export DEEPSEEK_API_KEY
if ! command -v docker >/dev/null 2>&1; then
    printf '错误：未找到 Docker。\n' >&2
    exit 2
fi
if ! docker info >/dev/null 2>&1; then
    printf '错误：Docker 服务不可用，或当前用户无访问权限。\n' >&2
    exit 2
fi

case "$BUILD_IMAGE" in
    1) docker build --network host -t "$IMAGE" "$RUNTIME_ROOT" || exit $? ;;
    0)
        if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
            printf '错误：镜像 %s 不存在，且已指定 --no-build。\n' "$IMAGE" >&2
            exit 2
        fi
        ;;
    auto)
        if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
            printf '未找到镜像 %s，开始构建……\n' "$IMAGE"
            docker build --network host -t "$IMAGE" "$RUNTIME_ROOT" || exit $?
        fi
        ;;
    *) printf '错误：BUILD_IMAGE 只能为 auto、1 或 0。\n' >&2; exit 2 ;;
esac

stop_containers() {
    local cid
    while read -r cid; do
        [[ -n "$cid" ]] && docker stop "$cid" >/dev/null 2>&1 || true
    done < <(docker ps -q --filter "label=col2inv.run=$RUN_ID")
}
trap 'printf "\n收到终止信号，停止本轮容器……\n" >&2; stop_containers; exit 130' INT TERM

final_status_from_log() {
    local log="$1"
    if [[ -f "$log" ]] && grep -q '^INFO | Success$' "$log"; then
        printf 'Success'
    elif [[ -f "$log" ]] && grep -q '^INFO | Fail$' "$log"; then
        printf 'Fail'
    else
        printf 'Error'
    fi
}

final_status_for_case() {
    local case_dir="$1" log="$2" saved_status=""
    if [[ -f "$case_dir/status.txt" ]]; then
        saved_status="$(tr -d '[:space:]' <"$case_dir/status.txt")"
        case "$saved_status" in
            Success|Fail)
                printf '%s' "$saved_status"
                return 0
                ;;
        esac
    fi
    final_status_from_log "$log"
}

cleanup_case_outputs() {
    local case_dir="$1"
    if rm -rf "$case_dir/result" "$case_dir/runner.log" \
        "$case_dir/status.txt" 2>/dev/null; then
        return 0
    fi

    # 兼容旧版本脚本留下的 root:root 结果：让容器以 root 身份只清理
    # 当前样例目录中的输出，不触碰测试输入文件。
    docker run --rm \
        --volume "$case_dir:/case" \
        --entrypoint sh \
        "$IMAGE" \
        -c 'rm -rf /case/result /case/runner.log /case/status.txt'
}

restore_result_ownership() {
    local case_dir="$1"
    [[ -d "$case_dir/result" ]] || return 0
    docker run --rm \
        --volume "$case_dir:/case" \
        --entrypoint chown \
        "$IMAGE" \
        -R "$HOST_UID:$HOST_GID" /case/result >/dev/null
}

run_case() {
    local id="$1" name="$2" case_dir file result_dir log status exit_code container
    case_dir="$TEST_ROOT/$id"
    file="test-03-01-$id.cbs"
    result_dir="$case_dir/result"
    log="$result_dir/test-03-01/$MODEL/$file/log.log"
    container="col2inv-${RUN_ID}-${id}"

    if [[ ! -f "$case_dir/$file" ]]; then
        printf 'Error\n' >"$case_dir/status.txt"
        printf '[%s/45] Result %s Error（缺少输入文件）\n' "$id" "$file"
        return 1
    fi

    # status.txt 是断点续跑的唯一保留条件。只要其内容严格为 Success
    # 或 Fail，就保留该用例的全部产物；Error、其他内容或文件缺失均重跑。
    if [[ "$FORCE" != 1 && -f "$case_dir/status.txt" ]]; then
        status="$(tr -d '[:space:]' <"$case_dir/status.txt")"
        case "$status" in
            Success|Fail)
                printf '[%s/45] Result %s %s\n' "$id" "$file" "$status"
                return 0
                ;;
        esac
    fi

    # 未完成产物会使 CoL2Inv 跳过运行，因此重跑前同时清理结果、运行日志和状态。
    if ! cleanup_case_outputs "$case_dir"; then
        printf 'Error\n' >"$case_dir/status.txt"
        printf '[%s/45] Result %s Error（无法清理旧输出）\n' "$id" "$file"
        return 1
    fi
    mkdir -p "$result_dir"
    printf '[%s/45] Running %s\n' "$id" "$file"

    docker run --rm \
        --name "$container" \
        --label "col2inv.run=$RUN_ID" \
        --env DEEPSEEK_API_KEY \
        --volume "$case_dir:/opt/col2inv/Benchmark/test-03-01:ro" \
        --volume "$result_dir:/results" \
        "$IMAGE" \
        --file "$file" \
        --dataset test-03-01 \
        --model "$MODEL" \
        --output /results \
        -- --proposal 5 --sample-timeout 0 \
        >"$case_dir/runner.log" 2>&1
    exit_code=$?

    # CoL2Inv 当前在容器内以 root 运行；完成后将挂载目录归还给启动脚本的用户。
    if ! restore_result_ownership "$case_dir"; then
        printf '[%s/45] Warning %s（结果已生成，但所有权修复失败）\n' "$id" "$file" >&2
    fi

    status="$(final_status_from_log "$log")"
    if [[ "$exit_code" -ne 0 && "$status" == Error ]]; then
        printf 'Error\n' >"$case_dir/status.txt"
    else
        printf '%s\n' "$status" >"$case_dir/status.txt"
    fi
    printf '[%s/45] Result %s %s\n' "$id" "$file" "$status"
    [[ "$status" != Error ]]
}

active_jobs() {
    jobs -pr | wc -l | tr -d ' '
}

while IFS=$'\t' read -r id name source; do
    [[ "$id" == id ]] && continue
    run_case "$id" "$name" &
    while (( $(active_jobs) >= JOBS )); do
        wait -n || true
    done
done <"$CASES_FILE"
wait || true
trap - INT TERM

summary_csv="$TEST_ROOT/summary.csv"
summary_txt="$TEST_ROOT/summary.txt"
printf 'id,name,file,status,proposal,running_time_seconds,log\n' >"$summary_csv"
success=0; fail=0; error=0; total=0
while IFS=$'\t' read -r id name source; do
    [[ "$id" == id ]] && continue
    file="test-03-01-$id.cbs"
    log="$TEST_ROOT/$id/result/test-03-01/$MODEL/$file/log.log"
    case_dir="$TEST_ROOT/$id"
    status="$(final_status_for_case "$case_dir" "$log")"
    proposal=""; elapsed=""
    if [[ -f "$log" ]]; then
        proposal="$(sed -n 's/^INFO | Proposal number: //p' "$log" | tail -1)"
        elapsed="$(sed -n 's/^INFO | Running time: //p' "$log" | tail -1)"
    fi
    case "$status" in
        Success) ((success+=1)) ;;
        Fail) ((fail+=1)) ;;
        *) ((error+=1)) ;;
    esac
    ((total+=1))
    printf '%s,"%s",%s,%s,%s,%s,%s\n' \
        "$id" "$name" "$file" "$status" "$proposal" "$elapsed" \
        "${log#$TEST_ROOT/}" >>"$summary_csv"
done <"$CASES_FILE"

rate="$(awk -v s="$success" -v t="$total" 'BEGIN { printf "%.2f", t ? 100*s/t : 0 }')"
verdict="未通过"
awk -v r="$rate" 'BEGIN { exit !(r >= 70.0) }' && verdict="通过"
{
    printf 'TEST-03-01 算法级循环不变式自动生成测试\n'
    printf '总用例数：%d\n' "$total"
    printf 'Success：%d\n' "$success"
    printf 'Fail：%d\n' "$fail"
    printf 'Error/未完成：%d\n' "$error"
    printf '生成成功率：%d/%d = %s%%\n' "$success" "$total" "$rate"
    printf '指标要求：成功率不低于70%%\n'
    printf '测试结论：%s\n' "$verdict"
    printf '明细文件：%s\n' "$summary_csv"
} | tee "$summary_txt"

[[ "$error" -eq 0 ]]

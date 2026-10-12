import { Alert, App as AntdApp, Modal, Select, Space, Switch, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { BTN, CADENCE_OPTIONS, ERR, TASK_CADENCE, TASK_SCOPE, formatCadence, recommendedMinCadenceSeconds } from "@/copy";
import { catalogApi, matrixApi, taskApi } from "@/data/api";
import type { MonitorTask, StoreOut } from "@/data/types";

interface ScopeModalProps {
  task: MonitorTask | null;
  open: boolean;
  onClose: () => void;
}

/** 选门店：全部（新登记门店自动加入）或指定若干门店；空列表=全部，与后端同构。 */
export function EditStoreScopeModal({ task, open, onClose }: ScopeModalProps) {
  const queryClient = useQueryClient();
  const { message } = AntdApp.useApp();
  const [clearanceOnly, setClearanceOnly] = useState(true);
  const stores = useQuery<StoreOut[]>({
    queryKey: ["stores", clearanceOnly ? "clearance" : "all"],
    queryFn: () => catalogApi.stores("", clearanceOnly ? "clearance" : "all"),
    enabled: open,
  });
  const [allStores, setAllStores] = useState(true);
  const [picked, setPicked] = useState<string[]>([]);

  useEffect(() => {
    if (!open || !task) return;
    setAllStores(task.all_stores);
    setPicked(task.store_codes ?? []);
  }, [open, task]);

  const save = useMutation({
    mutationFn: () => taskApi.update(task?.id ?? "", { store_codes: allStores ? [] : picked }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      message.success(TASK_SCOPE.saved);
      onClose();
    },
    onError: () => message.error(ERR.commonFailure),
  });

  const options = useMemo(
    () =>
      (stores.data ?? []).map((store) => ({
        value: store.store_code,
        label: `${store.store_code} · ${store.display_name}`,
      })),
    [stores.data],
  );

  return (
    <Modal
      open={open}
      title={task ? TASK_SCOPE.storeTitle(task.name) : TASK_SCOPE.storeAction}
      onCancel={onClose}
      onOk={() => save.mutate()}
      okText={TASK_SCOPE.save}
      cancelText={BTN.cancel}
      confirmLoading={save.isPending}
      okButtonProps={{ disabled: !task || (!allStores && picked.length === 0) }}
      destroyOnClose
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Space size={8}>
          <Switch checked={clearanceOnly} onChange={setClearanceOnly} />
          <Typography.Text>{TASK_SCOPE.clearanceOnly}</Typography.Text>
        </Space>
        <Space size={8}>
          <Switch checked={allStores} onChange={setAllStores} />
          <Typography.Text>{TASK_SCOPE.allStores}</Typography.Text>
        </Space>
        <Select
          mode="multiple"
          style={{ width: "100%" }}
          placeholder={TASK_SCOPE.storeAction}
          value={picked}
          onChange={setPicked}
          options={options}
          disabled={allStores}
          loading={stores.isLoading}
          optionFilterProp="label"
          maxTagCount="responsive"
        />
        <Typography.Text type="secondary">
          {!stores.isLoading && options.length === 0
            ? TASK_SCOPE.storesMissing
            : `${TASK_SCOPE.clearanceHint}${TASK_SCOPE.storeHint}`}
        </Typography.Text>
      </Space>
    </Modal>
  );
}

/** 选码：默认全部尺码；指定若干后，其它码不再检查也不计入结果。 */
export function EditSizeScopeModal({ task, open, onClose }: ScopeModalProps) {
  const queryClient = useQueryClient();
  const { message } = AntdApp.useApp();
  const sizes = useQuery<string[]>({
    queryKey: ["task-sizes", task?.id ?? ""],
    enabled: open && Boolean(task) && (task?.style_codes.length ?? 0) > 0,
    queryFn: async () => {
      // 尺码表来自各门店的结构；这里取任务货号在所有门店的并集（缺表也照样展示已知码）。
      const results = await Promise.allSettled(
        (task?.style_codes ?? []).map((code) => matrixApi.get(code.toUpperCase())),
      );
      const labels = new Set<string>();
      for (const result of results) {
        if (result.status !== "fulfilled") continue;
        for (const size of result.value.sizes) labels.add(size);
      }
      return [...labels].sort((a, b) => {
        const left = Number.parseFloat(a);
        const right = Number.parseFloat(b);
        if (Number.isFinite(left) && Number.isFinite(right) && left !== right) return left - right;
        return a.localeCompare(b);
      });
    },
  });
  const [allSizes, setAllSizes] = useState(true);
  const [picked, setPicked] = useState<string[]>([]);

  useEffect(() => {
    if (!open || !task) return;
    setAllSizes((task.size_labels ?? []).length === 0);
    setPicked(task.size_labels ?? []);
  }, [open, task]);

  const save = useMutation({
    mutationFn: () => taskApi.update(task?.id ?? "", { size_labels: allSizes ? [] : picked }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      message.success(TASK_SCOPE.saved);
      onClose();
    },
    onError: () => message.error(ERR.commonFailure),
  });

  const options = (sizes.data ?? []).map((size) => ({ value: size, label: size }));

  return (
    <Modal
      open={open}
      title={task ? TASK_SCOPE.sizeTitle(task.name) : TASK_SCOPE.sizeAction}
      onCancel={onClose}
      onOk={() => save.mutate()}
      okText={TASK_SCOPE.save}
      cancelText={BTN.cancel}
      confirmLoading={save.isPending}
      okButtonProps={{ disabled: !task || (!allSizes && picked.length === 0) }}
      destroyOnClose
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Space size={8}>
          <Switch checked={allSizes} onChange={setAllSizes} />
          <Typography.Text>{TASK_SCOPE.allSizes}</Typography.Text>
        </Space>
        <Select
          mode="multiple"
          style={{ width: "100%" }}
          placeholder={TASK_SCOPE.sizeAction}
          value={picked}
          onChange={setPicked}
          options={options}
          disabled={allSizes}
          loading={sizes.isLoading}
          maxTagCount="responsive"
        />
        <Typography.Text type="secondary">
          {sizes.isLoading
            ? TASK_SCOPE.sizesLoading
            : options.length === 0
              ? TASK_SCOPE.sizesMissing
              : TASK_SCOPE.sizeHint}
        </Typography.Text>
      </Space>
    </Modal>
  );
}

/** 改间隔：后端一直支持 cadence_seconds，之前前端没有入口（客户反馈"无法修改间隔"）。 */
export function EditCadenceModal({ task, open, onClose }: ScopeModalProps) {
  const queryClient = useQueryClient();
  const { message } = AntdApp.useApp();
  const [seconds, setSeconds] = useState<number>(1800);

  useEffect(() => {
    if (!open || !task) return;
    setSeconds(task.cadence_seconds);
  }, [open, task]);

  const save = useMutation({
    mutationFn: () => taskApi.update(task?.id ?? "", { cadence_seconds: seconds }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      if (task) void queryClient.invalidateQueries({ queryKey: ["task", task.id] });
      message.success(TASK_CADENCE.saved);
      onClose();
    },
    onError: () => message.error(ERR.commonFailure),
  });

  const advice = task
    ? recommendedMinCadenceSeconds(task.store_count ?? 0, task.style_count ?? task.style_codes.length)
    : 0;
  const belowAdvice = advice > 0 && seconds < advice;

  return (
    <Modal
      open={open}
      title={task ? TASK_CADENCE.title(task.name) : TASK_CADENCE.action}
      onCancel={onClose}
      onOk={() => save.mutate()}
      okText={TASK_SCOPE.save}
      cancelText={BTN.cancel}
      confirmLoading={save.isPending}
      okButtonProps={{ disabled: !task }}
      destroyOnClose
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Select
          style={{ width: "100%" }}
          value={seconds}
          onChange={setSeconds}
          options={CADENCE_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
        />
        {task ? (
          <Typography.Text type="secondary">
            {TASK_CADENCE.current(formatCadence(task.cadence_seconds))}
            {" · "}
            {TASK_CADENCE.hint}
          </Typography.Text>
        ) : null}
        {advice > 0 ? (
          <Alert
            type={belowAdvice ? "warning" : "info"}
            showIcon
            message={TASK_CADENCE.advice(advice)}
          />
        ) : null}
      </Space>
    </Modal>
  );
}

import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Result,
  Select,
  Space,
  Steps,
  Switch,
  Tag,
  Typography,
} from "antd";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { catalogApi, scanApi, taskApi } from "@/data/api";
import type { StoreOut, StyleOut } from "@/data/types";

const STEP_ITEMS = [
  { title: "选择货号" },
  { title: "选择门店" },
  { title: "设置周期" },
  { title: "确认并创建" },
];

interface TaskDraft {
  name: string;
  cadence_seconds: number;
  notify_on_restock: boolean;
  notify_on_sold_out: boolean;
  notify_on_price_change: boolean;
}

function parseCodes(input: string): string[] {
  const parts = input
    .split(/[\s,，;；]+/)
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean);
  return Array.from(new Set(parts));
}

export function TaskCreatePage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [form] = Form.useForm<TaskDraft>();
  const [codes, setCodes] = useState<string[]>([]);
  const [codeDraft, setCodeDraft] = useState("");
  const [storeCodes, setStoreCodes] = useState<string[]>([]);
  const [runImmediately, setRunImmediately] = useState(true);

  const stores = useQuery<StoreOut[]>({ queryKey: ["stores"], queryFn: () => catalogApi.stores() });
  const styles = useQuery<StyleOut[]>({ queryKey: ["styles"], queryFn: () => catalogApi.styles() });

  const create = useMutation({
    mutationFn: async (payload: TaskDraft) => {
      const task = await taskApi.create({
        ...payload,
        style_codes: codes,
        store_codes: storeCodes,
      });
      if (runImmediately) {
        const job = await scanApi.create({ task_id: task.id });
        return { task, jobId: job.id };
      }
      return { task, jobId: null as string | null };
    },
    onSuccess: (result) => {
      if (result.jobId) navigate(`/scans/${result.jobId}`);
      else navigate("/tasks");
    },
  });

  const storeOptions = useMemo(
    () =>
      (stores.data ?? []).map((store) => ({
        value: store.store_code,
        label: `${store.store_code} · ${store.display_name || "未命名门店"}`,
      })),
    [stores.data],
  );

  const addCodes = () => {
    const parsed = parseCodes(codeDraft);
    if (parsed.length === 0) return;
    setCodes((prev) => Array.from(new Set([...prev, ...parsed])));
    setCodeDraft("");
  };

  const submit = async () => {
    // The form stays mounted across every step, so the values survive the
    // round trip to the confirm page.
    const values = await form.validateFields();
    create.mutate(values);
  };

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card size="small">
        <Steps current={step} items={STEP_ITEMS} size="small" />
      </Card>

      {create.isError ? (
        <Alert type="error" showIcon message="创建失败" description={(create.error as Error).message} />
      ) : null}

      <Form
        form={form}
        layout="vertical"
        initialValues={{
          name: "新建监控任务",
          cadence_seconds: 1800,
          notify_on_restock: true,
          notify_on_sold_out: true,
          notify_on_price_change: true,
        }}
      >
        <div hidden={step !== 0}>
          <Card size="small" title="第一步：选择要监控的货号">
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <Space.Compact style={{ width: "100%" }}>
                <Input
                  value={codeDraft}
                  onChange={(event) => setCodeDraft(event.target.value)}
                  onPressEnter={addCodes}
                  placeholder="粘贴货号，支持空格、逗号、换行分隔，例如 DN3707-202 IO5031-600"
                />
                <Button onClick={addCodes}>添加</Button>
              </Space.Compact>
              <Select
                mode="multiple"
                allowClear
                style={{ width: "100%" }}
                placeholder="或从已登记货号中选择"
                value={codes}
                onChange={(value: string[]) => setCodes(value)}
                options={(styles.data ?? []).map((style) => ({
                  value: style.style_code,
                  label: `${style.style_code}${style.title && style.title !== style.style_code ? ` · ${style.title}` : ""}`,
                }))}
              />
              <Space wrap>
                {codes.map((code) => (
                  <Tag key={code} closable onClose={() => setCodes(codes.filter((item) => item !== code))}>
                    {code}
                  </Tag>
                ))}
              </Space>
              <Typography.Text type="secondary">
                共 {codes.length} 个货号。全国扫描会对每个货号 × 每家门店发起尺码级审核请求，请留意配额。
              </Typography.Text>
              <Button type="primary" disabled={codes.length === 0} onClick={() => setStep(1)}>
                下一步
              </Button>
            </Space>
          </Card>
        </div>

        <div hidden={step !== 1}>
          <Card size="small" title="第二步：选择门店">
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <Select
                mode="multiple"
                allowClear
                showSearch
                optionFilterProp="label"
                style={{ width: "100%" }}
                placeholder="选择门店（可多选）"
                value={storeCodes}
                onChange={(value: string[]) => setStoreCodes(value)}
                options={storeOptions}
                loading={stores.isLoading}
              />
              <Space>
                <Button
                  size="small"
                  onClick={() => setStoreCodes((stores.data ?? []).map((store) => store.store_code))}
                >
                  全选（全国扫描）
                </Button>
                <Button size="small" onClick={() => setStoreCodes([])}>
                  清空
                </Button>
              </Space>
              <Typography.Text type="secondary">
                已选 {storeCodes.length} 家门店。不选择门店代表监控全部已同步门店，之后新同步的门店会自动纳入；
                扫描时以任务保存的门店范围为准。
              </Typography.Text>
              <Space>
                <Button onClick={() => setStep(0)}>上一步</Button>
                <Button type="primary" onClick={() => setStep(2)}>
                  下一步
                </Button>
              </Space>
            </Space>
          </Card>
        </div>

        <div hidden={step !== 2}>
          <Card size="small" title="第三步：设置周期与提醒">
            <Form.Item label="任务名称" name="name" rules={[{ required: true, message: "请输入任务名称" }]}>
              <Input maxLength={120} />
            </Form.Item>
            <Form.Item
              label="监控周期"
              name="cadence_seconds"
              rules={[{ required: true }]}
              extra="周期越短消耗越多配额；默认 30 分钟。"
            >
              <InputNumber
                min={300}
                max={86400}
                step={300}
                style={{ width: 200 }}
                addonAfter="秒"
              />
            </Form.Item>
            <Form.Item label="提醒类型">
              <Space direction="vertical">
                <Form.Item name="notify_on_restock" valuePropName="checked" noStyle>
                  <Switch /> <span>补货提醒</span>
                </Form.Item>
                <Form.Item name="notify_on_sold_out" valuePropName="checked" noStyle>
                  <Switch /> <span>售罄提醒</span>
                </Form.Item>
                <Form.Item name="notify_on_price_change" valuePropName="checked" noStyle>
                  <Switch /> <span>变价提醒</span>
                </Form.Item>
              </Space>
            </Form.Item>
            <Space>
              <Button onClick={() => setStep(1)}>上一步</Button>
              <Button
                type="primary"
                onClick={async () => {
                  await form.validateFields();
                  setStep(3);
                }}
              >
                下一步
              </Button>
            </Space>
          </Card>
        </div>

        <div hidden={step !== 3}>
          <Card size="small" title="第四步：确认并创建">
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <Result
                status="info"
                title={`将监控 ${codes.length} 个货号 × ${storeCodes.length || "全部"} 家门店`}
                subTitle="每次运行的实际请求量取决于各门店尺码结构与价格可得性，暂无法可靠预估；配额以实际请求计量。"
                extra={
                  <Space direction="vertical">
                    <Space>
                      <Switch checked={runImmediately} onChange={setRunImmediately} />
                      <span>创建后立即扫描一次</span>
                    </Space>
                    <Space>
                      <Button onClick={() => setStep(2)}>上一步</Button>
                      <Button type="primary" loading={create.isPending} onClick={submit}>
                        创建任务
                      </Button>
                    </Space>
                  </Space>
                }
              />
            </Space>
          </Card>
        </div>
      </Form>
    </Space>
  );
}

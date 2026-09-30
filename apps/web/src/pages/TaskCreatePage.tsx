import {
  Alert,
  App as AntdApp,
  Button,
  Descriptions,
  Form,
  Input,
  Select,
  Space,
  Steps,
  Switch,
  Tag,
  Typography,
} from "antd";
import { BellOutlined, TagsOutlined } from "@ant-design/icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { NikeConnectionNotice, isCredentialActive, useNikeCredentialQuery } from "@/components/NikeCredentialCard";
import { PageHeader } from "@/components/PageHeader";
import { useHasEnabledChannel } from "@/components/PushChannelsCard";
import { SectionCard } from "@/components/SectionCard";
import { useAuth } from "@/auth/AuthContext";
import { BTN, CADENCE_OPTIONS, CATALOG, EMPTY, ERR, MSG, PUSH, WIZARD, formatCadence } from "@/copy";
import { catalogApi, scanApi, taskApi } from "@/data/api";
import type { StoreOut, StyleOut } from "@/data/types";
import { useIsMobile } from "@/hooks/useIsMobile";

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
  const isMobile = useIsMobile();
  const { message } = AntdApp.useApp();
  const { canWrite } = useAuth();
  const credential = useNikeCredentialQuery();
  const bound = isCredentialActive(credential.data?.status);
  const canCreate = canWrite && bound;
  const [step, setStep] = useState(0);
  const [form] = Form.useForm<TaskDraft>();
  const [codes, setCodes] = useState<string[]>([]);
  const [codeDraft, setCodeDraft] = useState("");
  const [storeCodes, setStoreCodes] = useState<string[]>([]);
  const [runImmediately, setRunImmediately] = useState(true);

  const stores = useQuery<StoreOut[]>({ queryKey: ["stores"], queryFn: () => catalogApi.stores() });
  const styles = useQuery<StyleOut[]>({ queryKey: ["styles"], queryFn: () => catalogApi.styles() });
  const hasEnabledChannel = useHasEnabledChannel();

  const create = useMutation({
    mutationFn: async (payload: TaskDraft) => {
      const task = await taskApi.create({
        ...payload,
        style_codes: codes,
        store_codes: storeCodes,
      });
      if (runImmediately) {
        const job = await scanApi.create({ task_id: task.id });
        return { task, jobId: job.id as string | null, cadence: payload.cadence_seconds };
      }
      return { task, jobId: null as string | null, cadence: payload.cadence_seconds };
    },
    onSuccess: (result) => {
      if (result.jobId) {
        message.success(MSG.taskCreatedChecking());
        navigate(`/scans/${result.jobId}`);
      } else {
        message.success(MSG.taskCreated(formatCadence(result.cadence)));
        navigate("/tasks");
      }
    },
  });

  const storeOptions = useMemo(
    () =>
      (stores.data ?? []).map((store) => ({
        value: store.store_code,
        label: `${store.store_code} · ${store.display_name || store.store_code}`,
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
    // 表单常驻挂载：切步不丢值（hidden 切换保留现实现）。
    const values = await form.validateFields();
    create.mutate(values);
  };

  const stepItems = [
    { title: "选择货号和门店", icon: <TagsOutlined /> },
    { title: "设置频率并确认", icon: <BellOutlined /> },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader title="新建监控任务" />
      {isMobile ? (
        <Typography.Text type="secondary">
          步骤 {step + 1}/2 · {WIZARD.steps[step]}
        </Typography.Text>
      ) : (
        <Steps current={step} size="small" items={stepItems} />
      )}

      {!canWrite ? <Alert type="info" showIcon message={ERR.forbidden} /> : null}
      <NikeConnectionNotice action="创建监控任务或启动检查" />

      {create.isError ? (
        <Alert
          type="error"
          showIcon
          message={ERR.taskCreateFailed}
          description={ERR.commonFailure}
          action={
            <Button size="small" loading={create.isPending} onClick={() => void submit()}>
              重试
            </Button>
          }
        />
      ) : null}

      <Form<TaskDraft>
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
          <SectionCard title={WIZARD.steps[0]}>
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <Space.Compact style={{ width: "100%" }}>
                <Input
                  value={codeDraft}
                  onChange={(event) => setCodeDraft(event.target.value)}
                  onPressEnter={addCodes}
                  placeholder={WIZARD.stylePlaceholder}
                />
                <Button onClick={addCodes}>{BTN.add}</Button>
              </Space.Compact>
              <Select
                mode="multiple"
                allowClear
                style={{ width: "100%" }}
                placeholder={WIZARD.styleSelectPlaceholder}
                value={codes}
                onChange={(value: string[]) => setCodes(value)}
                options={(styles.data ?? []).map((style) => ({
                  value: style.style_code,
                  label: `${style.style_code}${style.title && style.title !== style.style_code ? ` · ${style.title}` : ""}`,
                }))}
              />
              <Space wrap>
                {codes.map((code) => (
                  <Tag
                    key={code}
                    closable
                    onClose={() => setCodes(codes.filter((item) => item !== code))}
                  >
                    {code}
                  </Tag>
                ))}
              </Space>

              <Select
                mode="multiple"
                allowClear
                showSearch
                optionFilterProp="label"
                style={{ width: "100%" }}
                placeholder={WIZARD.storePlaceholder}
                value={storeCodes}
                onChange={(value: string[]) => setStoreCodes(value)}
                options={storeOptions}
                loading={stores.isLoading}
              />
              <Space>
                <span className="desktop-only">
                  <Button
                    size="small"
                    onClick={() => setStoreCodes((stores.data ?? []).map((store) => store.store_code))}
                  >
                    {BTN.selectAll}
                  </Button>
                </span>
                <Button size="small" onClick={() => setStoreCodes([])}>
                  {BTN.clear}
                </Button>
              </Space>
              <Typography.Text type="secondary">{EMPTY.wizardStoreHint}</Typography.Text>

              {stores.isSuccess && (stores.data ?? []).length === 0 ? (
                <Alert
                  type="info"
                  showIcon
                  message={CATALOG.emptyHint}
                  action={<Link to="/account">去连接</Link>}
                />
              ) : null}

              <Form.Item
                label={WIZARD.taskNameLabel}
                name="name"
                rules={[{ required: true, message: "请输入任务名称" }]}
              >
                <Input maxLength={120} />
              </Form.Item>

              <Space direction="vertical" size={4}>
                <Typography.Text type="secondary">{WIZARD.styleCount(codes.length)}</Typography.Text>
                {codes.length === 0 ? (
                  <Typography.Text type="secondary">{EMPTY.wizardNoStyles}</Typography.Text>
                ) : null}
                <Button type="primary" disabled={codes.length === 0} onClick={() => setStep(1)}>
                  {BTN.nextStep}
                </Button>
              </Space>
            </Space>
          </SectionCard>
        </div>

        <div hidden={step !== 1}>
          <SectionCard title={WIZARD.steps[1]}>
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <Form.Item
                label={WIZARD.frequencyLabel}
                name="cadence_seconds"
                rules={[{ required: true }]}
              >
                <Select
                  style={{ maxWidth: 320 }}
                  options={CADENCE_OPTIONS.map((option) => ({
                    value: option.value,
                    label: option.label,
                  }))}
                />
              </Form.Item>
              <Space direction="vertical" size={8}>
                <Form.Item name="notify_on_restock" valuePropName="checked" noStyle>
                  <Switch /> <span>{WIZARD.notifyRestock}</span>
                </Form.Item>
                <Form.Item name="notify_on_sold_out" valuePropName="checked" noStyle>
                  <Switch /> <span>{WIZARD.notifySoldOut}</span>
                </Form.Item>
                <Form.Item name="notify_on_price_change" valuePropName="checked" noStyle>
                  <Switch /> <span>{WIZARD.notifyPriceChange}</span>
                </Form.Item>
              </Space>
              <Form.Item noStyle shouldUpdate>
                {() => {
                  const values = form.getFieldsValue();
                  const notifyOn = Boolean(
                    values.notify_on_restock ||
                      values.notify_on_sold_out ||
                      values.notify_on_price_change,
                  );
                  if (!notifyOn || hasEnabledChannel !== false) return null;
                  return (
                    <Alert
                      type="info"
                      showIcon
                      message={PUSH.noChannelHint}
                      action={<Link to="/account">{PUSH.noChannelLink}</Link>}
                    />
                  );
                }}
              </Form.Item>
              <Space>
                <Switch checked={runImmediately} onChange={setRunImmediately} />
                <span>{WIZARD.runImmediately}</span>
              </Space>

              <Form.Item noStyle shouldUpdate>
                {() => {
                  const values = form.getFieldsValue();
                  const cadence = values.cadence_seconds ?? 1800;
                  const notify: string[] = [];
                  if (values.notify_on_restock) notify.push(WIZARD.notifyRestock);
                  if (values.notify_on_sold_out) notify.push(WIZARD.notifySoldOut);
                  if (values.notify_on_price_change) notify.push(WIZARD.notifyPriceChange);
                  return (
                    <Descriptions size="small" column={1} title={WIZARD.summaryLabel}>
                      <Descriptions.Item label="货号">
                        {WIZARD.summaryStyles(codes.length)}
                      </Descriptions.Item>
                      <Descriptions.Item label="门店">
                        {storeCodes.length === 0
                          ? WIZARD.summaryStoresAll
                          : WIZARD.summaryStores(storeCodes.length)}
                      </Descriptions.Item>
                      <Descriptions.Item label={WIZARD.frequencyLabel}>
                        {formatCadence(cadence)}
                      </Descriptions.Item>
                      <Descriptions.Item label="提醒">
                        {notify.length === 0 ? (
                          <Tag>已关闭全部提醒</Tag>
                        ) : (
                          <Space size={4} wrap>
                            {notify.map((label) => (
                              <Tag key={label}>{label}</Tag>
                            ))}
                          </Space>
                        )}
                      </Descriptions.Item>
                    </Descriptions>
                  );
                }}
              </Form.Item>

              <Typography.Text type="secondary">{WIZARD.quotaNote}</Typography.Text>

              <Space>
                <Button onClick={() => setStep(0)}>{BTN.prevStep}</Button>
                <Button
                  type="primary"
                  disabled={!canCreate}
                  loading={create.isPending}
                  onClick={() => void submit()}
                >
                  {BTN.createTask}
                </Button>
              </Space>
            </Space>
          </SectionCard>
        </div>
      </Form>
    </Space>
  );
}

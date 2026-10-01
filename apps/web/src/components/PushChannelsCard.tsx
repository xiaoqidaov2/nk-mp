import { App as AntdApp, Button, Divider, Form, Input, Modal, Select, Space, Switch, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { BTN, ERR, PUSH, TASK_LIST, formatDate } from "@/copy";
import { ApiError } from "@/data/client";
import { notificationApi } from "@/data/api";
import type {
  DingTalkLink,
  NotificationChannel,
  NotificationDelivery,
  NotificationPlatform,
} from "@/data/types";

import { SectionCard } from "./SectionCard";

//: dingtalk_app 由「钉钉机器人」绑定自动创建，不放进手填列表。
const PLATFORM_KEYS = (Object.keys(PUSH.platforms) as NotificationPlatform[]).filter(
  (key) => key !== "dingtalk_app",
);

/** 平台表单字段；顺序即表单顺序，与后端 4.1 表一致。 */
const PLATFORM_FIELDS: Record<NotificationPlatform, string[]> = {
  dingtalk: ["access_token", "secret"],
  dingtalk_app: ["robot_code", "robot_secret", "conversation_id"],
  feishu: ["token"],
  wecombot: ["key_or_url"],
  schan: ["sendkey"],
  bark: ["device_key", "server"],
  mailto: ["smtp_host", "smtp_port", "username", "password", "from", "to"],
  custom: ["url"],
};

/** 可以留空的字段：其余字段空着提交时后端会说明缺哪个。 */
const OPTIONAL_FIELDS: Record<string, true> = {
  secret: true,
  server: true,
  smtp_port: true,
  //: 留空即进入“@ 机器人 自动绑定”流程。
  conversation_id: true,
};

const STATE_LABEL: Record<NotificationDelivery["state"], string> = {
  SENT: PUSH.stateSent,
  PENDING: PUSH.statePending,
  FAILED: PUSH.stateFailed,
};

const STATE_COLOR: Record<NotificationDelivery["state"], string> = {
  SENT: "success",
  PENDING: "warning",
  FAILED: "error",
};

/** 任务页判断"提醒有没有落点"：null 表示还没查到，不提示。 */
export function useHasEnabledChannel(): boolean | null {
  const channels = useQuery<NotificationChannel[]>({
    queryKey: ["notification-channels"],
    queryFn: () => notificationApi.channels(),
  });
  if (!channels.data) return null;
  return channels.data.some((channel) => channel.enabled);
}

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : ERR.commonFailure;
}

/**
 * 消息推送卡：渠道列表 + 最近推送。
 *
 * 渠道属于当前登录用户；测试发送与重发都走服务端，失败原因以服务端
 * 固定文案为准，卡片不做二次解释。
 */
export function PushChannelsCard() {
  const { message, modal } = AntdApp.useApp();
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [form] = Form.useForm();
  const platform = (Form.useWatch("platform", form) as NotificationPlatform | undefined) ?? "dingtalk";

  const channels = useQuery<NotificationChannel[]>({
    queryKey: ["notification-channels"],
    queryFn: () => notificationApi.channels(),
  });
  const [linkCode, setLinkCode] = useState("");
  const links = useQuery<DingTalkLink[]>({
    queryKey: ["dingtalk-links"],
    queryFn: () => notificationApi.dingtalkLinks(),
  });
  const deliveries = useQuery<NotificationDelivery[]>({
    queryKey: ["notification-deliveries"],
    queryFn: () => notificationApi.deliveries(10),
  });

  const invalidateChannels = () => {
    void queryClient.invalidateQueries({ queryKey: ["notification-channels"] });
  };

  const create = useMutation({
    mutationFn: (payload: { platform: NotificationPlatform; name: string; fields: Record<string, string> }) =>
      notificationApi.createChannel(payload),
    onSuccess: () => {
      invalidateChannels();
      setAddOpen(false);
      form.resetFields();
    },
    onError: (error) => message.error(errorText(error)),
  });

  const toggle = useMutation({
    mutationFn: (payload: { id: string; enabled: boolean }) =>
      notificationApi.updateChannel(payload.id, { enabled: payload.enabled }),
    onSuccess: invalidateChannels,
    onError: (error) => message.error(errorText(error)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => notificationApi.removeChannel(id),
    onSuccess: invalidateChannels,
    onError: (error) => message.error(errorText(error)),
  });

  const test = useMutation({
    mutationFn: (id: string) => notificationApi.testChannel(id),
    onSuccess: (result) => {
      if (result.ok) {
        message.success(PUSH.testOk);
      } else {
        message.error(PUSH.testFailed(result.error ?? ERR.commonFailure));
      }
      invalidateChannels();
    },
    onError: (error) => message.error(PUSH.testFailed(errorText(error))),
  });

  const retry = useMutation({
    mutationFn: (id: number) => notificationApi.retryDelivery(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notification-deliveries"] });
      invalidateChannels();
    },
    onError: (error) => message.error(errorText(error)),
  });

  const bindCode = useMutation({
    mutationFn: () => notificationApi.dingtalkLinkCode(),
    onSuccess: (result) => setLinkCode(result.code),
    onError: (error) => message.error(errorText(error)),
  });

  const unbind = useMutation({
    mutationFn: (id: string) => notificationApi.removeDingTalkLink(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["dingtalk-links"] });
      invalidateChannels();
    },
    onError: (error) => message.error(errorText(error)),
  });

  const confirmUnbind = (link: DingTalkLink) => {
    modal.confirm({
      title: PUSH.dingtalkUnbindConfirmTitle,
      content: PUSH.dingtalkUnbindConfirmBody,
      okText: PUSH.dingtalkUnbindConfirmOk,
      cancelText: BTN.cancel,
      okButtonProps: { danger: true },
      onOk: () => unbind.mutateAsync(link.id),
    });
  };

  const confirmRemove = (channel: NotificationChannel) => {
    modal.confirm({
      title: PUSH.removeConfirmTitle,
      content: PUSH.removeConfirmBody,
      okText: PUSH.removeConfirmOk,
      cancelText: BTN.cancel,
      okButtonProps: { danger: true },
      onOk: () => remove.mutateAsync(channel.id),
    });
  };

  const submit = async () => {
    const values = await form.validateFields();
    const fields: Record<string, string> = {};
    for (const name of PLATFORM_FIELDS[platform]) {
      const value = String(values.fields?.[name] ?? "").trim();
      if (value) fields[name] = value;
    }
    create.mutate({
      platform,
      name: String(values.name ?? "").trim(),
      fields,
    });
  };

  const items = channels.data ?? [];
  const records = deliveries.data ?? [];

  return (
    <SectionCard
      title={PUSH.cardTitle}
      description={PUSH.cardNote}
      loading={channels.isLoading}
      extra={
        <Button type="primary" onClick={() => setAddOpen(true)}>
          {PUSH.addChannel}
        </Button>
      }
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        {items.length === 0 ? (
          <Typography.Text type="secondary">{PUSH.empty}</Typography.Text>
        ) : (
          items.map((channel) => (
            <Space key={channel.id} direction="vertical" size={6} style={{ width: "100%" }}>
              <Space wrap size={8} align="center">
                <Typography.Text strong>{PUSH.platforms[channel.platform].label}</Typography.Text>
                {channel.name ? <Typography.Text>{channel.name}</Typography.Text> : null}
                <Typography.Text type="secondary">{channel.target_hint}</Typography.Text>
              </Space>
              <Space wrap size={8} align="center">
                <Switch
                  checked={channel.enabled}
                  checkedChildren="启用"
                  unCheckedChildren="停用"
                  loading={toggle.isPending && toggle.variables?.id === channel.id}
                  onChange={(checked) => toggle.mutate({ id: channel.id, enabled: checked })}
                />
                {channel.last_error ? (
                  <Typography.Text type="warning">{channel.last_error}</Typography.Text>
                ) : channel.last_ok_at ? (
                  <Typography.Text type="secondary">
                    {PUSH.lastOk(formatDate(channel.last_ok_at))}
                  </Typography.Text>
                ) : null}
                <Button
                  size="small"
                  loading={test.isPending && test.variables === channel.id}
                  onClick={() => test.mutate(channel.id)}
                >
                  {PUSH.sendTest}
                </Button>
                <Button size="small" danger onClick={() => confirmRemove(channel)}>
                  {PUSH.remove}
                </Button>
              </Space>
            </Space>
          ))
        )}

        <Divider style={{ margin: "8px 0" }} />
        <Typography.Title level={5} style={{ margin: 0 }}>
          {PUSH.dingtalkTitle}
        </Typography.Title>
        <Typography.Text type="secondary">{PUSH.dingtalkNote}</Typography.Text>
        <Space wrap size={8} align="center">
          <Button size="small" loading={bindCode.isPending} onClick={() => bindCode.mutate()}>
            {PUSH.dingtalkBind}
          </Button>
          {linkCode ? (
            <Typography.Text>{PUSH.dingtalkBindTip(linkCode)}</Typography.Text>
          ) : null}
        </Space>
        <Typography.Text type="secondary">{PUSH.dingtalkKeywords}</Typography.Text>
        {(links.data ?? []).length === 0 ? (
          <Typography.Text type="secondary">{PUSH.dingtalkLinksEmpty}</Typography.Text>
        ) : (
          (links.data ?? []).map((link) => (
            <Space key={link.id} wrap size={8} align="center">
              <Tag>{link.conversation_type === 1 ? PUSH.dingtalkLinkDm : PUSH.dingtalkLinkGroup}</Tag>
              <Typography.Text>{link.conversation_title || PUSH.dingtalkLinkDm}</Typography.Text>
              <Button size="small" danger onClick={() => confirmUnbind(link)}>
                {PUSH.dingtalkUnbind}
              </Button>
            </Space>
          ))
        )}

        <Divider style={{ margin: "8px 0" }} />
        <Typography.Title level={5} style={{ margin: 0 }}>
          {PUSH.recentTitle}
        </Typography.Title>
        {records.length === 0 ? (
          <Typography.Text type="secondary">{PUSH.recentEmpty}</Typography.Text>
        ) : (
          records.map((record) => (
            <Space key={record.id} wrap size={8} align="center">
              <Typography.Text type="secondary">
                {`${formatDate(record.created_at)} · ${record.task_name ?? TASK_LIST.manualCheck} · ${record.channel_name}`}
              </Typography.Text>
              <Tag color={STATE_COLOR[record.state]}>{STATE_LABEL[record.state]}</Tag>
              {record.last_error ? (
                <Typography.Text type="warning">{record.last_error}</Typography.Text>
              ) : null}
              <Button
                size="small"
                loading={retry.isPending && retry.variables === record.id}
                onClick={() => retry.mutate(record.id)}
              >
                {PUSH.retry}
              </Button>
            </Space>
          ))
        )}
      </Space>

      <Modal
        open={addOpen}
        title={PUSH.addChannel}
        footer={null}
        onCancel={() => {
          setAddOpen(false);
          form.resetFields();
        }}
      >
        <Form form={form} layout="vertical" initialValues={{ platform: "dingtalk" }}>
          <Form.Item name="platform" label={PUSH.platformLabel}>
            <Select
              options={PLATFORM_KEYS.map((key) => ({ value: key, label: PUSH.platforms[key].label }))}
            />
          </Form.Item>
          <Form.Item name="name" label={PUSH.nameLabel}>
            <Input maxLength={64} />
          </Form.Item>
          {PLATFORM_FIELDS[platform].map((field) => (
            <Form.Item
              key={field}
              name={["fields", field]}
              label={PUSH.fields[field as keyof typeof PUSH.fields]}
              required={!OPTIONAL_FIELDS[field]}
              preserve={false}
            >
              <Input autoComplete="off" />
            </Form.Item>
          ))}
          <Typography.Text type="secondary">{PUSH.platforms[platform].hint}</Typography.Text>
          <Space style={{ marginTop: 16, width: "100%", justifyContent: "flex-end" }}>
            <Button
              onClick={() => {
                setAddOpen(false);
                form.resetFields();
              }}
            >
              {BTN.cancel}
            </Button>
            <Button type="primary" loading={create.isPending} onClick={() => void submit()}>
              {PUSH.saveChannel}
            </Button>
          </Space>
        </Form>
      </Modal>
    </SectionCard>
  );
}

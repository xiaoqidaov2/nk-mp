import { App as AntdApp, Button, Input, Space, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { BTN, ERR, PUSH } from "@/copy";
import { ApiError } from "@/data/client";
import { notificationApi } from "@/data/api";
import type { DingTalkLink, DingTalkRobot } from "@/data/types";

import { SectionCard } from "./SectionCard";

/**
 * 钉钉企业内部机器人（可选）：独立于「消息推送」的一张卡。
 *
 * 只影响「在钉钉里直接给平台发指令」；只收提醒的话用
 * 「消息推送」里的「钉钉群机器人」渠道就够了，与这张卡无关。
 */
export function DingTalkRobotCard() {
  const { message, modal } = AntdApp.useApp();
  const queryClient = useQueryClient();
  const [robotKey, setRobotKey] = useState("");
  const [robotSecret, setRobotSecret] = useState("");
  const [editingRobot, setEditingRobot] = useState(false);

  const robot = useQuery<DingTalkRobot>({
    queryKey: ["dingtalk-robot"],
    queryFn: () => notificationApi.dingtalkRobot(),
  });
  const links = useQuery<DingTalkLink[]>({
    queryKey: ["dingtalk-links"],
    queryFn: () => notificationApi.dingtalkLinks(),
  });

  const invalidateChannels = () => {
    void queryClient.invalidateQueries({ queryKey: ["notification-channels"] });
  };

  const saveRobot = useMutation({
    mutationFn: () =>
      notificationApi.saveDingTalkRobot({
        robot_code: robotKey.trim(),
        robot_secret: robotSecret.trim(),
      }),
    onSuccess: (result) => {
      setRobotKey("");
      setRobotSecret("");
      setEditingRobot(false);
      void queryClient.invalidateQueries({ queryKey: ["dingtalk-robot"] });
      void queryClient.invalidateQueries({ queryKey: ["dingtalk-links"] });
      invalidateChannels();
      message.success(PUSH.dingtalkSaved(result.robot_code));
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : ERR.commonFailure),
  });

  const removeRobot = useMutation({
    mutationFn: () => notificationApi.removeDingTalkRobot(),
    onSuccess: () => {
      setEditingRobot(false);
      void queryClient.invalidateQueries({ queryKey: ["dingtalk-robot"] });
      void queryClient.invalidateQueries({ queryKey: ["dingtalk-links"] });
      invalidateChannels();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : ERR.commonFailure),
  });

  const unbind = useMutation({
    mutationFn: (id: string) => notificationApi.removeDingTalkLink(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["dingtalk-links"] });
      invalidateChannels();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : ERR.commonFailure),
  });

  const confirmRemoveRobot = () => {
    modal.confirm({
      title: PUSH.dingtalkRemoveConfirmTitle,
      content: PUSH.dingtalkRemoveConfirmBody,
      okText: PUSH.dingtalkRemoveConfirmOk,
      cancelText: BTN.cancel,
      okButtonProps: { danger: true },
      onOk: () => removeRobot.mutateAsync(),
    });
  };

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

  const configured = robot.data?.configured ?? false;

  return (
    <SectionCard
      title={PUSH.dingtalkTitle}
      loading={robot.isLoading}
      extra={
        <Tag color={configured ? "blue" : "default"}>
          {configured ? PUSH.dingtalkConfiguredTag : PUSH.dingtalkOptionalTag}
        </Tag>
      }
    >
      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        <Typography.Text type="secondary">{PUSH.dingtalkNote}</Typography.Text>
        {configured && !editingRobot ? (
          <Space wrap size={8} align="center">
            <Typography.Text>{PUSH.dingtalkSaved(robot.data?.robot_code ?? "")}</Typography.Text>
            <Button size="small" onClick={() => setEditingRobot(true)}>
              {PUSH.dingtalkChange}
            </Button>
            <Button size="small" danger onClick={confirmRemoveRobot}>
              {PUSH.dingtalkRemove}
            </Button>
          </Space>
        ) : (
          <Space wrap size={8} align="center">
            <Input
              style={{ width: 200 }}
              placeholder={PUSH.dingtalkKey}
              value={robotKey}
              autoComplete="off"
              onChange={(event) => setRobotKey(event.target.value)}
            />
            <Input
              style={{ width: 240 }}
              placeholder={PUSH.dingtalkSecret}
              value={robotSecret}
              autoComplete="off"
              onChange={(event) => setRobotSecret(event.target.value)}
            />
            <Button
              size="small"
              type="primary"
              disabled={!robotKey.trim() || !robotSecret.trim()}
              loading={saveRobot.isPending}
              onClick={() => saveRobot.mutate()}
            >
              {PUSH.dingtalkSave}
            </Button>
          </Space>
        )}
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
      </Space>
    </SectionCard>
  );
}

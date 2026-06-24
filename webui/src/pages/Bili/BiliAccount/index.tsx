import services from '@/services/luboman';
import {
  ActionType,
  ModalForm,
  ProFormDigit,
  ProFormDependency,
  ProFormRadio,
  ProFormText,
  ProFormTextArea,
  ProTable,
} from '@ant-design/pro-components';
import { ReloadOutlined } from '@ant-design/icons';
import { Avatar, Badge, Button, Popconfirm, Space } from 'antd';
import React, { useRef, useState } from 'react';

const { listBiliAccount, addBiliAccount, delBiliAccount } =
  services.BiliAccount;

const BiliAccountList: React.FC = () => {
  const actionRef = useRef<ActionType>();
  const [createOpen, setCreateOpen] = useState(false);

  const handleDelete = async (id: number) => {
    await delBiliAccount(id);
    actionRef.current?.reload();
  };

  return (
    <>
    <ProTable<API.BiliAccountInfo>
      headerTitle="B 站投稿账号"
      actionRef={actionRef}
      rowKey="id"
      search={false}
      pagination={false}
      size="middle"
      options={false}
      toolBarRender={() => [
        <Button
          key="reload"
          icon={<ReloadOutlined />}
          onClick={() => actionRef.current?.reload()}
        >
          刷新
        </Button>,
        <Button
          key="create"
          type="primary"
          onClick={() => setCreateOpen(true)}
        >
          新建
        </Button>,
      ]}
      request={async () => {
        try {
          const data = await listBiliAccount();
          return { data: data || [], success: true };
        } catch {
          return { data: [], success: false };
        }
      }}
      columns={[
        { title: 'ID', dataIndex: 'id', width: 60 },
        {
          title: '头像',
          dataIndex: 'account_avatar',
          width: 70,
          render: (_, r) =>
            r.account_avatar ? (
              <Avatar src={r.account_avatar} size={32} />
            ) : (
              <Avatar size={32}>{(r.account_name || '?').slice(0, 1)}</Avatar>
            ),
        },
        { title: '账号名', dataIndex: 'account_name', ellipsis: true },
        {
          title: 'Cookies 文件路径',
          dataIndex: 'bili_cookies_filepath',
          ellipsis: true,
        },
        {
          title: '状态',
          dataIndex: 'state_active',
          width: 90,
          render: (_, r) => (
            <Badge
              status={r.state_active === 1 ? 'success' : 'default'}
              text={r.state_active === 1 ? '启用' : '停用'}
            />
          ),
        },
        {
          title: '操作',
          dataIndex: 'option',
          valueType: 'option',
          width: 90,
          render: (_, record) => (
            <Space>
              <Popconfirm
                title="确认删除该账号？"
                description="将停用该账号（state_active 置 0）"
                onConfirm={() => record.id && handleDelete(record.id)}
              >
                <a>删除</a>
              </Popconfirm>
            </Space>
          ),
        },
      ]}
    />

    <ModalForm
      title="新建 B 站账号"
      width={560}
      open={createOpen}
      onOpenChange={setCreateOpen}
      modalProps={{ destroyOnClose: true }}
      initialValues={{ cookieType: 'cookies', state_active: 1 }}
      onFinish={async (values) => {
        const payload: Partial<API.BiliAccountInfo> = {
          state_active: values.state_active,
        };
        if (values.cookieType === 'filepath') {
          payload.bili_cookies_filepath = values.bili_cookies_filepath;
        } else {
          payload.bili_cookies = values.bili_cookies;
        }
        await addBiliAccount(payload);
        actionRef.current?.reload();
        return true;
      }}
    >
      <ProFormRadio.Group
        name="cookieType"
        label="登录方式"
        options={[
          { label: 'Cookies 字符串', value: 'cookies' },
          { label: 'Cookies 文件路径', value: 'filepath' },
        ]}
      />
      <ProFormDependency name={['cookieType']}>
        {({ cookieType }) =>
          cookieType === 'filepath' ? (
            <ProFormText
              name="bili_cookies_filepath"
              label="Cookies 文件路径"
              placeholder="biliup-rs 生成的 cookies.json 路径"
              rules={[{ required: true, message: '请输入文件路径' }]}
            />
          ) : (
            <ProFormTextArea
              name="bili_cookies"
              label="Cookies 字符串"
              placeholder="SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx;"
              rules={[{ required: true, message: '请输入 Cookies' }]}
              fieldProps={{ autoSize: { minRows: 3, maxRows: 6 } }}
            />
          )
        }
      </ProFormDependency>
      <ProFormDigit
        name="state_active"
        label="状态"
        min={0}
        max={1}
        extra="1 启用 / 0 停用，默认 1"
      />
    </ModalForm>
    </>
  );
};

export default BiliAccountList;

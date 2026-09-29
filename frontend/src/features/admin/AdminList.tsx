import { Table, type TableProps } from "antd";
import { useMemo, type HTMLAttributes, type ReactNode } from "react";
import "./admin-lists.css";

export function AdminList({
  title,
  extra,
  children,
  className = "",
  "aria-label": ariaLabel,
}: {
  title?: string;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
  "aria-label"?: string;
}): React.JSX.Element {
  return (
    <section
      className={`admin-list ${className}`}
      aria-label={ariaLabel ?? title}
    >
      {(title || extra) && (
        <header className="admin-list-heading">
          {title && <h2>{title}</h2>}
          {extra}
        </header>
      )}
      {children}
    </section>
  );
}

export function AdminListFooter({
  children,
}: {
  children: ReactNode;
}): React.JSX.Element {
  return <footer className="admin-list-footer">{children}</footer>;
}

type AdminTableProps<T extends object> = Omit<
  TableProps<T>,
  "size" | "pagination" | "bordered" | "components"
> & { bodyTestId?: string };

export function AdminTable<T extends object>({
  className = "",
  bodyTestId,
  locale,
  scroll = { x: "max-content" },
  ...props
}: AdminTableProps<T>): React.JSX.Element {
  const components = useMemo<TableProps<T>["components"]>(() => {
    if (bodyTestId === undefined) return undefined;
    const Body = (attributes: HTMLAttributes<HTMLTableSectionElement>) => (
      <tbody {...attributes} data-testid={bodyTestId} />
    );
    return { body: { wrapper: Body } };
  }, [bodyTestId]);

  return (
    <div className="admin-table-scroll" data-scroll-x="table">
      <Table<T>
        {...props}
        className={`admin-table ${className}`}
        size="small"
        pagination={false}
        bordered={false}
        locale={{ emptyText: "No records match these filters.", ...locale }}
        scroll={scroll}
        {...(components === undefined ? {} : { components })}
      />
    </div>
  );
}

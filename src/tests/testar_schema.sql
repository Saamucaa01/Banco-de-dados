-- ============================================================================
-- Conecta Campus — verificação do esquema no próprio PostgreSQL/Supabase
-- (Responsável: Samuel)
--
-- COMO USAR: depois de executar schema_supabase.sql, cole este arquivo no SQL
-- Editor do Supabase e execute. Se algo estiver errado, o script para com uma
-- mensagem "FALHOU: ...". Se tudo passar, a última linha exibida é
-- "OK: todas as verificações do esquema passaram".
--
-- Tudo roda dentro de uma transação que termina em ROLLBACK: nenhum dado de
-- teste fica gravado. Os comandos de escrita rodam com o papel "anon", o
-- mesmo da chave pública usada pelo aplicativo.
-- ============================================================================

BEGIN;

-- Auxiliar: executa um comando que DEVE falhar com o SQLSTATE esperado.
-- O comando roda em subtransação, então a falha não contamina o resto.
CREATE FUNCTION pg_temp.espera_erro(p_sql TEXT, p_sqlstate TEXT, p_rotulo TEXT)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        EXECUTE p_sql;
    EXCEPTION WHEN OTHERS THEN
        IF SQLSTATE = p_sqlstate THEN
            RETURN;
        END IF;
        RAISE EXCEPTION 'FALHOU: % — esperado SQLSTATE %, veio % (%)',
            p_rotulo, p_sqlstate, SQLSTATE, SQLERRM;
    END;
    RAISE EXCEPTION 'FALHOU: % — o comando deveria ter sido recusado', p_rotulo;
END;
$$;

CREATE FUNCTION pg_temp.confere(p_condicao BOOLEAN, p_rotulo TEXT)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    IF p_condicao IS NOT TRUE THEN
        RAISE EXCEPTION 'FALHOU: %', p_rotulo;
    END IF;
END;
$$;

-- 1. RLS ligado nas três tabelas ---------------------------------------------
SELECT pg_temp.confere(
    (SELECT count(*) FROM pg_class
      WHERE relnamespace = 'public'::regnamespace
        AND relname IN ('monitorias', 'agendamentos', 'historico_situacoes')
        AND relrowsecurity) = 3,
    'RLS deve estar ligado em monitorias, agendamentos e historico_situacoes');

SET LOCAL ROLE anon;

-- 2. Monitoria válida e restrições CHECK --------------------------------------
INSERT INTO public.monitorias (disciplina, monitor, dia_semana, hora_inicio, hora_fim, local, vagas)
VALUES ('Banco de Dados', 'Samuel', 0, '14:00', '16:00', 'Lab 03', 1);

SELECT pg_temp.espera_erro(
    $q$INSERT INTO public.monitorias (disciplina, monitor, dia_semana, hora_inicio, hora_fim, local, vagas)
       VALUES ('X', 'Y', 0, '14:00', '16:00', 'L', 0)$q$,
    '23514', 'vagas = 0 deve violar CHECK');
SELECT pg_temp.espera_erro(
    $q$INSERT INTO public.monitorias (disciplina, monitor, dia_semana, hora_inicio, hora_fim, local, vagas)
       VALUES ('X', 'Y', 6, '14:00', '16:00', 'L', 1)$q$,
    '23514', 'dia_semana = 6 (domingo) deve violar CHECK');
SELECT pg_temp.espera_erro(
    $q$INSERT INTO public.monitorias (disciplina, monitor, dia_semana, hora_inicio, hora_fim, local, vagas)
       VALUES ('X', 'Y', 1, '16:00', '14:00', 'L', 1)$q$,
    '23514', 'hora_fim antes de hora_inicio deve violar CHECK');
SELECT pg_temp.espera_erro(
    $q$INSERT INTO public.monitorias (disciplina, monitor, dia_semana, hora_inicio, hora_fim, local, vagas)
       VALUES ('  ', 'Y', 1, '14:00', '16:00', 'L', 1)$q$,
    '23514', 'disciplina em branco deve violar CHECK');

-- 3. A chave pública não escreve direto em agendamentos/histórico -------------
SELECT pg_temp.espera_erro(
    $q$INSERT INTO public.agendamentos (monitoria_id, aluno_matricula, aluno_nome, data, assunto)
       SELECT id, '1', 'A', '2030-01-07', 'x' FROM public.monitorias LIMIT 1$q$,
    '42501', 'INSERT direto em agendamentos deve ser negado ao anon');
SELECT pg_temp.espera_erro(
    $q$UPDATE public.agendamentos SET situacao = 'cancelado'$q$,
    '42501', 'UPDATE direto em agendamentos deve ser negado ao anon');
SELECT pg_temp.espera_erro(
    $q$DELETE FROM public.agendamentos$q$,
    '42501', 'DELETE em agendamentos deve ser negado ao anon');
SELECT pg_temp.espera_erro(
    $q$INSERT INTO public.historico_situacoes (agendamento_id, situacao_nova) VALUES (1, 'solicitado')$q$,
    '42501', 'INSERT direto em historico_situacoes deve ser negado ao anon');
SELECT pg_temp.espera_erro(
    $q$DELETE FROM public.monitorias$q$,
    '42501', 'DELETE em monitorias deve ser negado ao anon');

-- 4. Agendamento + histórico gravados juntos -----------------------------------
CREATE TEMP TABLE _ctx AS
SELECT (SELECT id FROM public.monitorias LIMIT 1) AS mon_id;
GRANT SELECT ON _ctx TO anon;

SELECT public.adicionar_agendamento_com_historico(
    (SELECT mon_id FROM _ctx), '2025001', 'Ana', DATE '2030-01-07', 'SQL');

SELECT pg_temp.confere(
    (SELECT count(*) FROM public.agendamentos) = 1
    AND (SELECT count(*) FROM public.historico_situacoes
          WHERE situacao_anterior IS NULL AND situacao_nova = 'solicitado') = 1,
    'a criação deve gravar 1 agendamento e 1 linha inicial de histórico');

-- 5. Duplicidade ativa (mesmo aluno, monitoria e data) ------------------------
SELECT pg_temp.espera_erro(
    $q$SELECT public.adicionar_agendamento_com_historico(
           (SELECT mon_id FROM _ctx), '2025001', 'Ana', DATE '2030-01-07', 'outro')$q$,
    '23505', 'agendamento ativo duplicado deve violar o índice único parcial');

-- 6. Limite de vagas dentro da transação (a monitoria tem 1 vaga) ---------------
SELECT pg_temp.espera_erro(
    $q$SELECT public.adicionar_agendamento_com_historico(
           (SELECT mon_id FROM _ctx), '2025002', 'Beto', DATE '2030-01-07', 'SQL')$q$,
    'CC001', 'monitoria lotada deve ser recusada com CC001');
SELECT pg_temp.confere(
    (SELECT count(*) FROM public.agendamentos) = 1,
    'a recusa por lotação não pode deixar agendamento gravado');

-- outra data da mesma monitoria tem vaga
SELECT public.adicionar_agendamento_com_historico(
    (SELECT mon_id FROM _ctx), '2025002', 'Beto', DATE '2030-01-14', 'SQL');

-- 7. Monitoria inexistente ------------------------------------------------------
SELECT pg_temp.espera_erro(
    $q$SELECT public.adicionar_agendamento_com_historico(
           -1, '2025003', 'Caio', DATE '2030-01-07', 'SQL')$q$,
    'P0002', 'monitoria inexistente deve ser recusada');

-- 8. Atomicidade: falha no histórico desfaz o agendamento -----------------------
-- p_situacao_anterior inválida viola o CHECK da tabela de histórico DEPOIS que
-- o agendamento já foi inserido dentro da função.
SELECT pg_temp.espera_erro(
    $q$SELECT public.adicionar_agendamento_com_historico(
           (SELECT mon_id FROM _ctx), '2025009', 'Teste', DATE '2030-01-21', 'SQL',
           'solicitado', NULL, 'situacao_invalida', 'solicitado', NULL)$q$,
    '23514', 'falha ao gravar o histórico deve ser recusada');
SELECT pg_temp.confere(
    (SELECT count(*) FROM public.agendamentos WHERE aluno_matricula = '2025009') = 0,
    'ATOMICIDADE: nenhum agendamento pode sobrar quando o histórico falha');

-- 9. Mudança de situação atômica e com histórico --------------------------------
SELECT public.registrar_mudanca_situacao(
    (SELECT id FROM public.agendamentos WHERE aluno_matricula = '2025001'),
    'solicitado', 'confirmado');

SELECT pg_temp.confere(
    (SELECT situacao FROM public.agendamentos WHERE aluno_matricula = '2025001') = 'confirmado'
    AND (SELECT count(*) FROM public.historico_situacoes h
           JOIN public.agendamentos a ON a.id = h.agendamento_id
          WHERE a.aluno_matricula = '2025001') = 2,
    'a mudança deve atualizar a situação e acrescentar 1 linha de histórico');

SELECT pg_temp.espera_erro(
    $q$SELECT public.registrar_mudanca_situacao(
           (SELECT id FROM public.agendamentos WHERE aluno_matricula = '2025001'),
           'solicitado', 'cancelado')$q$,
    '40001', 'situação anterior desatualizada deve gerar conflito');

SELECT pg_temp.espera_erro(
    $q$SELECT public.registrar_mudanca_situacao(
           (SELECT id FROM public.agendamentos WHERE aluno_matricula = '2025001'),
           'confirmado', 'situacao_invalida')$q$,
    '23514', 'situação nova inválida deve violar CHECK');
SELECT pg_temp.confere(
    (SELECT situacao FROM public.agendamentos WHERE aluno_matricula = '2025001') = 'confirmado'
    AND (SELECT count(*) FROM public.historico_situacoes h
           JOIN public.agendamentos a ON a.id = h.agendamento_id
          WHERE a.aluno_matricula = '2025001') = 2,
    'ATOMICIDADE: mudança recusada não pode alterar situação nem histórico');

SELECT pg_temp.espera_erro(
    $q$SELECT public.registrar_mudanca_situacao(-1, NULL, 'confirmado')$q$,
    'P0002', 'agendamento inexistente deve ser recusado');

-- 10. Cancelar libera a vaga ------------------------------------------------------
SELECT public.registrar_mudanca_situacao(
    (SELECT id FROM public.agendamentos WHERE aluno_matricula = '2025001'),
    'confirmado', 'cancelado');
SELECT public.adicionar_agendamento_com_historico(
    (SELECT mon_id FROM _ctx), '2025002', 'Beto', DATE '2030-01-07', 'SQL');

RESET ROLE;
ROLLBACK;

SELECT 'OK: todas as verificações do esquema passaram' AS resultado;

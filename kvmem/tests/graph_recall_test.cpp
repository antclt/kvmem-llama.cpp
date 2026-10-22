#include "kvmem/graph_recall.hpp"
#include "kvmem/raw_kv_store.hpp"
#include "kvmem/kvmem_store.hpp"
#include <algorithm>
#include <cmath>
#include <iostream>
#include <numeric>
#include <stdexcept>

namespace {
void require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
void close(double actual, double expected, double tolerance = 1e-12) {
    require(std::abs(actual-expected) <= tolerance, "independent reference mismatch");
}
template<class F> void rejects(F f) {
    bool rejected = false;
    try { f(); } catch (const std::exception&) { rejected = true; }
    require(rejected, "invalid transaction was accepted");
}
void frames_and_scores() {
    auto frame = kvmem::query_score_frame(12, 4, 2, 0, 0, {2});
    require(frame.score_mask == std::vector<uint8_t>({1,1,1}), "mandatory changed softmax denominator");
    require(frame.transport_mask == std::vector<uint8_t>({1,1,0}), "mandatory transported graph mass");
    auto small = kvmem::query_score_frame(11,4,4,1,1,{0,2});
    require(small.score_mask == std::vector<uint8_t>({1,1,1}) &&
            small.transport_mask == std::vector<uint8_t>({0,0,0}) && small.counts.back() == 3,
            "small-history or partial-tail domain mismatch");
    auto constrained = kvmem::query_score_frame(12,4,2,1,1,{0,2});
    require(constrained.score_mask == std::vector<uint8_t>({0,1,0}), "keep-band score mask mismatch");
    kvmem::RawKvStore keys({1, 2, 0, 4});
    const float a[]{8,0}, b[]{0,4}, c[]{40,0};
    keys.write_layer_mean_sum(0,4,0,a);
    keys.write_layer_mean_sum(4,4,0,b);
    keys.write_layer_mean_sum(8,4,0,c);
    const float q[]{2,0, 0,2}; const uint32_t rows[]{2};
    const auto result = kvmem::page_softmax_scores(keys,{q,rows,1,2,1,1,2},frame);
    // Naive long-double reference, independent softmax and exact FP16 operands.
    long double expected[3]{};
    const long double logits[2][3]{{4/std::sqrt(2.L),0,20/std::sqrt(2.L)},
                                  {0,2/std::sqrt(2.L),0}};
    for (const auto& line : logits) {
        long double denominator = 0;
        for (auto value : line) denominator += std::exp(value);
        for (size_t i=0;i<3;++i) expected[i] += std::exp(line[i])/denominator;
    }
    for (size_t i=0;i<3;++i) close(result[i],double(expected[i]));
    require(result[1] > result[0], "mandatory-domain ranking regression");
    frame.score_mask[2] = 0;
    const auto wrong_domain = kvmem::page_softmax_scores(keys,{q,rows,1,2,1,1,2},frame);
    require(wrong_domain[0] > wrong_domain[1], "fixture does not discriminate the score domain");
    close(std::accumulate(result.begin(),result.end(),0.),2);
    frame.score_mask.assign(3,0);
    const auto empty = kvmem::page_softmax_scores(keys,{q,rows,1,2,1,1,2},frame);
    close(std::accumulate(empty.begin(),empty.end(),0.),0);
    close(kvmem::recall_fp16(1.00048828125f),1);
    close(kvmem::recall_fp16(1.00146484375f),1.001953125);
    close(kvmem::recall_fp16(std::ldexp(1.f,-24)),std::ldexp(1.,-24));
    close(kvmem::recall_fp16(std::ldexp(1.f,-25)),0);
    rejects([] { kvmem::recall_fp16(65536); });
}
void gqa_and_large_domain() {
    kvmem::RawKvStore keys({2,1,0,1});
    for (uint32_t l=0;l<2;++l) {
        const float a[]{1}, b[]{-1};
        keys.write_layer_mean_sum(0,1,l,a); keys.write_layer_mean_sum(1,1,l,b);
    }
    auto frame = kvmem::query_score_frame(2,1,1,0,0,{});
    const float q[]{1,-1, 2,-2}; const uint32_t rows[]{1,1};
    const auto result=kvmem::page_softmax_scores(keys,{q,rows,2,1,2,1,1},frame);
    close(result[0],.5); close(result[1],.5);
    // Boundary sizes must all normalize over the complete history.
    for (const uint32_t n : {8192u,8193u,32768u,32769u}) {
        kvmem::RawKvStore large({1,1,0,1});
        const float value[]{1};
        for (uint32_t b=0;b<n;++b) large.write_layer_mean_sum(b,1,0,value);
        auto f=kvmem::query_score_frame(n,1,1,0,0,{});
        const float query[]{1}; const uint32_t count[]{1};
        auto scores=kvmem::page_softmax_scores(large,{query,count,1,1,1,1,1},f);
        close(scores.front(),1./n); close(scores.back(),1./n);
        close(std::accumulate(scores.begin(),scores.end(),0.),1,2e-12);
    }
}
void graph_lifecycle() {
    constexpr size_t limit=1u<<20;
    kvmem::GraphSnapshot graph; graph.block_tokens=64;
    graph=kvmem::prepare_graph_accept(graph,0,64,{},limit);
    graph=kvmem::prepare_graph_accept(graph,64,65,{{64,{{0,.8},{64,.2}}}},limit);
    require(graph.chunks.empty() && graph.partial.samples==1, "partial source sealed early");
    const auto partial_checkpoint=graph;
    graph=kvmem::prepare_graph_accept(graph,65,128,{},limit);
    require(graph.chunks.size()==1 && graph.partial.samples==0 && graph.completed_through==128,
            "unsampled acceptance did not seal the source");
    close(graph.chunks[0]->at(0).edges[0].mass,.8);
    graph=kvmem::prepare_graph_accept(graph,128,192,{{128,{{64,1}}}},limit);
    auto frame=kvmem::query_score_frame(192,64,2,0,0,{});
    auto result=kvmem::propagate_query_scores({1,0,1},frame,graph);
    close(result[0],.95); close(result[1],.2); close(result[2],.85);
    close(std::accumulate(result.begin(),result.end(),0.),2);
    require(partial_checkpoint.chunks.empty() && partial_checkpoint.partial.samples==1,
            "later acceptance mutated an immutable checkpoint");
    frame.transport_mask[0]=0;
    result=kvmem::propagate_query_scores({8,0,1},frame,graph);
    close(result[0],8); close(result[1],.15); close(result[2],.85);
    rejects([&] { kvmem::prepare_graph_accept(graph,192,193,{{192,{{256,1}}}},limit); });
    rejects([&] { kvmem::prepare_graph_accept(graph,192,193,{{192,{{0,.2}}}},limit); });
    rejects([&] { kvmem::prepare_graph_accept(graph,192,256,{},1); });
    require(graph.accepted_through==192, "failed graph preparation changed its input");
    // Aggregate before top-k: an initially runner-up target can win later.
    kvmem::GraphSnapshot top; top.block_tokens=4;
    top=kvmem::prepare_graph_accept(top,0,12,{},limit);
    top=kvmem::prepare_graph_accept(top,12,14,
        {{12,{{0,.6},{4,.4}}},{13,{{4,.4},{8,.6}}}},limit,1);
    top=kvmem::prepare_graph_accept(top,14,16,{},limit,1);
    require(top.chunks[0]->at(0).edges[0].target==4,"top-k happened before source aggregation");
    close(top.chunks[0]->at(0).edges[0].mass,.4);
}
void feedback_and_pressure() {
    kvmem::AttentionFeedback feedback;
    feedback.observe_prefill(0,64,64,{{63,{{0,.2},{32,.8}}}},{0,32});
    auto initial=feedback.pressure_scores({0,32}); close(initial[0],.2); close(initial[1],.8);
    feedback.observe_output({{0,.75},{32,.25}});
    const auto checkpoint=feedback.snapshot();
    const auto preview=feedback.pressure_scores({0,32});
    close(preview[0],.75); close(preview[1],.25);
    require(feedback.snapshot().output.empty() && feedback.snapshot().output_samples==1,
            "pressure preview committed or decayed state");
    feedback.finish_output(); feedback.finish_output();
    close(feedback.pressure_scores({0,32})[0],.75);
    feedback.observe_output({{0,.25},{32,.75}});
    close(feedback.pressure_scores({0,32})[0],.625);
    feedback.restore(checkpoint);
    close(feedback.pressure_scores({0,32})[0],.75);
    // Fallback is evaluated before fixed/mandatory nodes are filtered.
    close(feedback.pressure_scores({0})[0],.75);
    feedback.observe_prefill(64,128,32,{{127,{{0,1}}}},{0});
    require(feedback.snapshot().prefill_end==64,"partially accepted prefill changed feedback");
    const auto picked=kvmem::pressure_selection({0,2,3},{.1,.8,0},{0},2);
    require(picked==std::vector<uint32_t>({0,2}),"pressure reopened cold history or lost a mandatory page");
    const auto recency=kvmem::pressure_selection({0,2,3},{0,0,0},{0},2);
    require(recency==std::vector<uint32_t>({0,3}),"pressure recency fill mismatch");
    rejects([] { kvmem::pressure_selection({0},{1},{0,1},1); });
}
void accepted_sampling() {
    std::vector<int32_t> tokens(35,42);
    tokens[0]=10; tokens[9]=11; tokens[26]=10; tokens[30]=11;
    auto plan=kvmem::accepted_output_samples({},tokens.data(),35,10,11);
    require(plan.rows==std::vector<uint32_t>({25}) && plan.state.eligible_tokens==20 && !plan.state.thinking,
        "thinking markers or rejected suffix polluted output sampling");
    auto prefix=kvmem::accepted_output_samples({},tokens.data(),26,10,11);
    auto suffix=kvmem::accepted_output_samples(prefix.state,tokens.data()+26,9,10,11);
    require(prefix.rows==plan.rows && suffix.rows.empty() && suffix.state.eligible_tokens==plan.state.eligible_tokens,
        "output sampling depends on GPU batch boundaries");
    require(kvmem::prefill_sample_rows(0,128,0,0,1024)==std::vector<uint32_t>({31,63,95,127}),
        "prefill sample stride differs from reference");
    require(kvmem::prefill_sample_rows(128,128,128,128,4096).empty(),"prefill interval ignored");
    require(!kvmem::prefill_sample_rows(128,128,128,720,1024).empty(),"near-capacity sample missed page slack");
}
void fixed_picker() {
    kvmem::KvMemStoreConfig config;config.block_tokens=4;config.select_budget=16;
    config.sink_blocks=1;config.recent_blocks=1;
    kvmem::KvMemStore store(config);store.register_append(24);
    store.set_media_ranges({{4,9},{9,13}});
    require(store.query_fixed_blocks({3})==std::vector<uint32_t>({0,1,2,3}),
        "overlapping image boundary groups did not close backwards");
    store.set_retrieval_scores({0,0,0,0,9,8});
    require(store.pick_graph_blocks({3})==store.query_fixed_blocks({3}),
        "actual picker differs from the graph transport fixed set");
    rejects([&]{store.pick_scored_blocks({0,3,4,5},{0,1,9,8},{0,3},4);});
}
void statistics_budget() {
    auto global=std::make_shared<kvmem::GraphStatisticsBudget>(40000);
    auto first=std::make_shared<kvmem::GraphStatisticsBudget>(35000,global);
    auto second=std::make_shared<kvmem::GraphStatisticsBudget>(35000,global);
    {
        kvmem::GraphSnapshot a,b;a.set_budget(first);b.set_budget(second);
        std::vector<kvmem::GraphMessageSpan> spans(1000);
        for(size_t i=0;i<spans.size();++i) spans[i]={i*8,i*8+8,true};
        a.set_messages(spans);
        const auto before=global->used();
        rejects([&]{b.set_messages(spans);});
        require(global->used()==before && !b.messages,"failed global admission changed its snapshot");
        rejects([&]{auto copy=a;});
        require(global->used()==before,"failed snapshot allocation leaked a budget lease");
    }
    require(!global->used() && !first->used() && !second->used(),"retired graph snapshots retained budget charges");
}
void message_locality() {
    kvmem::GraphSnapshot graph;graph.block_tokens=64;
    graph=kvmem::prepare_graph_accept(graph,0,192,{},1u<<20);
    auto frame=kvmem::query_score_frame(192,64,2,0,0,{});
    graph.set_messages(std::vector<kvmem::GraphMessageSpan>{{0,64,true},{64,192,true}});
    const auto crossed=kvmem::propagate_query_scores({1,0,0},frame,graph);
    close(crossed[0],.9875);close(crossed[1],.0125);
    graph.set_messages(std::vector<kvmem::GraphMessageSpan>{{0,192,true}});
    const auto same=kvmem::propagate_query_scores({1,0,0},frame,graph);
    close(same[0],.95);close(same[1],.05);
}
} // namespace
int main() {
    try {
        frames_and_scores(); gqa_and_large_domain(); graph_lifecycle(); feedback_and_pressure(); accepted_sampling(); fixed_picker(); statistics_budget(); message_locality();
        std::cout << "graph recall numerical and accepted-state checks passed\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n'; return 1;
    }
}
